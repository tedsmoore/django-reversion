"""Revision management for django-reversion."""

from __future__ import unicode_literals
import warnings
from collections import defaultdict
from functools import wraps, partial
from itertools import chain
from threading import local
from weakref import WeakValueDictionary
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.db.models import Max
from django.db.models.query import QuerySet
from django.db.models.signals import post_save
from django.utils.encoding import force_text
from reversion.compat import remote_field
from reversion.errors import RevisionManagementError, RegistrationError
from reversion.signals import post_revision_context_end


class VersionAdapter(object):

    """
    Adapter class for serializing a registered model.
    """

    def __init__(self, model):
        self.model = model

    fields = None
    """
    Field named to include in the serialized data.

    Set to `None` to include all model fields.
    """

    exclude = ()
    """
    Field names to exclude from the serialized data.
    """

    def get_fields_to_serialize(self):
        """
        Returns an iterable of field names to serialize in the version data.
        """
        opts = self.model._meta.concrete_model._meta
        fields = (
            field.name
            for field
            in opts.local_fields + opts.local_many_to_many
        ) if self.fields is None else self.fields
        fields = (opts.get_field(field) for field in fields if field not in self.exclude)
        for field in fields:
            if remote_field(field):
                yield field.name
            else:
                yield field.attname

    follow = ()
    """
    Foreign-key relationships to follow when saving a version of this model.

    `ForeignKey`, `ManyToManyField` and reversion `ForeignKey` relationships
    are all supported. Any property that returns a `Model` or `QuerySet`
    are also supported.
    """

    def get_followed_relations(self, obj):
        """
        Returns an iterable of related models that should be included in the revision data.

        `obj` - A model instance.
        """
        for relationship in self.follow:
            # Clear foreign key cache.
            try:
                related_field = obj._meta.get_field(relationship)
            except models.FieldDoesNotExist:
                pass
            else:
                if isinstance(related_field, models.ForeignKey):
                    if hasattr(obj, related_field.get_cache_name()):
                        delattr(obj, related_field.get_cache_name())
            # Get the referenced obj(s).
            try:
                related = getattr(obj, relationship)
            except ObjectDoesNotExist:  # pragma: no cover
                continue
            if isinstance(related, models.Model):
                yield related
            elif isinstance(related, (models.Manager, QuerySet)):
                for related_obj in related.all():
                    yield related_obj
            elif related is not None:  # pragma: no cover
                raise TypeError((
                    "Cannot follow the relationship {relationship}. "
                    "Expected a model or QuerySet, found {related}"
                ).format(
                    relationship=relationship,
                    related=related,
                ))

    format = "json"
    """
    The name of a Django serialization format to use when saving the version.
    """

    def get_serialization_format(self):
        """
        Returns the name of a Django serialization format to use when saving the version.
        """
        return self.format

    for_concrete_model = True
    """
    If `True` (default), then proxy models will be saved under the same content
    type as their concrete model. If `False`, then proxy models will be saved
    under their own content type, effectively giving proxy models their own
    distinct history.
    """

    signals = (post_save,)
    """
    Django signals that trigger saving a version.

    The model version will be saved at the end of the outermost revision block.
    """

    eager_signals = ()
    """
    Django signals that trigger saving a version.

    The model version will be saved immediately, making it suitable for signals
    that trigger before a model is deleted.
    """

    def get_all_signals(self):
        """
        Returns an iterable of all signals that trigger saving a version.
        """
        return chain(self.signals, self.eager_signals)

    def get_serialized_data(self, obj):
        """
        Returns a string of serialized data for the given model instance.

        `obj` - A model instance.
        """
        return serializers.serialize(
            self.get_serialization_format(),
            (obj,),
            fields=list(self.get_fields_to_serialize()),
        )

    def get_version_id(self, obj):
        """
        Returns a tuple of (app_label, model_name, object_id) for the given model instance.

        `obj` - A model instance.
        """
        if self.for_concrete_model:
            opts = obj._meta.concrete_model._meta
        else:
            opts = obj._meta
        return (opts.app_label, opts.model_name, force_text(obj.pk))

    def get_version_data(self, obj):
        """
        Creates a dict of version data to be saved to the version model.

        `obj` - A model instance.
        """
        app_label, model_name, object_id = self.get_version_id(obj)
        return {
            "app_label": app_label,
            "model_name": model_name,
            "object_id": object_id,
            "db": obj._state.db,
            "format": self.get_serialization_format(),
            "serialized_data": self.get_serialized_data(obj),
            "object_repr": force_text(obj),
        }


class RevisionContextStackFrame(object):

    def __init__(self, manage_manually, is_invalid, user, comment, ignore_duplicates, manager_objects, meta):
        # Block-scoped properties.
        self.manage_manually = manage_manually
        self.is_invalid = is_invalid
        # Revision-scoped properties.
        self.user = user
        self.comment = comment
        self.ignore_duplicates = ignore_duplicates
        self.manager_objects = manager_objects
        self.meta = meta

    def fork(self, manage_manually):
        return RevisionContextStackFrame(
            manage_manually,
            self.is_invalid,
            self.user,
            self.comment,
            self.ignore_duplicates,
            defaultdict(dict, {
                manager: objects.copy()
                for manager, objects
                in self.manager_objects.items()
            }),
            self.meta.copy(),
        )

    def join(self, other_frame):
        if not other_frame.is_invalid:
            # Copy back all revision-scoped properties.
            self.user = other_frame.user
            self.comment = other_frame.comment
            self.ignore_duplicates = other_frame.ignore_duplicates
            self.manager_objects = other_frame.manager_objects
            self.meta = other_frame.meta


class RevisionContextManager(local):

    def __init__(self):
        self._stack = []
        self._db_depths = defaultdict(int)

    def is_active(self):
        """
        Returns whether there is an active revision for this thread.
        """
        return bool(self._stack)

    @property
    def _current_frame(self):
        if not self.is_active():
            raise RevisionManagementError("There is no active revision for this thread")
        return self._stack[-1]

    def _start(self, manage_manually, db):
        if self.is_active():
            self._stack.append(self._current_frame.fork(manage_manually))
        else:
            self._stack.append(RevisionContextStackFrame(
                manage_manually,
                is_invalid=False,
                user=None,
                comment="",
                ignore_duplicates=False,
                manager_objects=defaultdict(dict),
                meta=[],
            ))
        self._db_depths[db] += 1

    def _invalidate(self):
        self._current_frame.is_invalid = True

    def _end(self, db):
        stack_frame = self._current_frame
        self._db_depths[db] -= 1
        try:
            if self._db_depths[db] == 0 and not stack_frame.is_invalid:
                for manager, objects in stack_frame.manager_objects.items():
                    post_revision_context_end.send(
                        sender=manager,
                        objects=[obj for obj in objects.values() if not isinstance(obj, dict)],
                        serialized_objects=[obj for obj in objects.values() if isinstance(obj, dict)],
                        user=stack_frame.user,
                        comment=stack_frame.comment,
                        meta=stack_frame.meta,
                        ignore_duplicates=stack_frame.ignore_duplicates,
                        db=db,
                    )
        finally:
            self._stack.pop()
            if self._stack:
                self._current_frame.join(stack_frame)

    # Block-scoped properties.

    def is_managing_manually(self):
        """Returns whether this revision context has manual management enabled."""
        return self._current_frame.manage_manually

    def is_invalid(self):
        """Checks whether this revision is invalid."""
        return self._current_frame.is_invalid

    # Revision-scoped properties.

    def set_user(self, user):
        """Sets the current user for the revision."""
        self._current_frame.user = user

    def get_user(self):
        """Gets the current user for the revision."""
        return self._current_frame.user

    def set_comment(self, comment):
        """Sets the comments for the revision."""
        self._current_frame.comment = comment

    def get_comment(self):
        """Gets the current comment for the revision."""
        return self._current_frame.comment

    def set_ignore_duplicates(self, ignore_duplicates):
        """Sets whether to ignore duplicate revisions."""
        self._current_frame.ignore_duplicates = ignore_duplicates

    def get_ignore_duplicates(self):
        """Gets whether to ignore duplicate revisions."""
        return self._current_frame.ignore_duplicates

    def add_to_context(self, revision_manager, obj):
        """
        Adds an object to the current revision.
        """
        adapter = revision_manager.get_adapter(obj.__class__)
        self._current_frame.manager_objects[revision_manager][adapter.get_version_id(obj)] = obj

    def add_to_context_eager(self, revision_manager, obj):
        """
        Adds a dict of pre-serialized version data to the current revision
        """
        for relation in revision_manager._follow_relationships(obj):
            adapter = revision_manager.get_adapter(relation.__class__)
            version_data = adapter.get_version_data(relation)
            self._current_frame.manager_objects[revision_manager][adapter.get_version_id(relation)] = version_data

    def add_meta(self, cls, **kwargs):
        """Adds a model of meta information to the current revision."""
        self._current_frame.meta.append((cls(**kwargs)))

    # High-level context management.

    def create_revision(self, manage_manually=False, db=None):
        """
        Marks up a block of code as requiring a revision to be created.

        The returned context manager can also be used as a decorator.
        """
        return RevisionContext(self, manage_manually, db)


class RevisionContext(object):

    def __init__(self, context_manager, manage_manually, db):
        self._context_manager = context_manager
        self._manage_manually = manage_manually
        self._db = db
        self._transaction = transaction.atomic(using=db)

    def __enter__(self):
        self._transaction.__enter__()
        self._context_manager._start(self._manage_manually, self._db)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is not None:
                self._context_manager._invalidate()
        finally:
            try:
                self._context_manager._end(self._db)
            finally:
                self._transaction.__exit__(exc_type, exc_value, traceback)

    def __call__(self, func):
        @wraps(func)
        def do_revision_context(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
        return do_revision_context


# A shared, thread-safe context manager.
revision_context_manager = RevisionContextManager()


class RevisionManager(object):

    """Manages the configuration and creation of revisions."""

    _created_managers = WeakValueDictionary()

    @classmethod
    def get_created_managers(cls):
        """Returns all created revision managers."""
        return list(cls._created_managers.items())

    @classmethod
    def get_manager(cls, manager_slug):
        """Returns the manager with the given slug."""
        if manager_slug in cls._created_managers:
            return cls._created_managers[manager_slug]
        raise RegistrationError("No revision manager exists with the slug %r" % manager_slug)  # pragma: no cover

    def __init__(self, manager_slug, revision_context_manager=revision_context_manager):
        """Initializes the revision manager."""
        # Check the slug is unique for this revision manager.
        if manager_slug in RevisionManager._created_managers:  # pragma: no cover
            raise RegistrationError("A revision manager has already been created with the slug %r" % manager_slug)
        # Store a reference to this manager.
        self.__class__._created_managers[manager_slug] = self
        # Store config params.
        self._manager_slug = manager_slug
        self._registered_models = {}
        self._revision_context_manager = revision_context_manager
        # Proxies to common context methods.
        self._revision_context = revision_context_manager.create_revision()

    # Registration methods.

    def is_registered(self, model):
        """
        Checks whether the given model has been registered with this revision
        manager.
        """
        return model in self._registered_models

    def get_registered_models(self):
        """Returns an iterable of all registered models."""
        return list(self._registered_models.keys())

    def register(self, model=None, adapter_cls=VersionAdapter, **field_overrides):
        """Registers a model with this revision manager."""
        # Return a class decorator if model is not given
        if model is None:
            return partial(self.register, adapter_cls=adapter_cls, **field_overrides)
        # Prevent multiple registration.
        if self.is_registered(model):
            raise RegistrationError("{model} has already been registered with django-reversion".format(
                model=model,
            ))
        # Perform any customization.
        if field_overrides:
            adapter_cls = type(adapter_cls.__name__, (adapter_cls,), field_overrides)
        # Perform the registration.
        adapter_obj = adapter_cls(model)
        self._registered_models[model] = adapter_obj
        # Connect to the selected signals of the model.
        for signal in adapter_obj.get_all_signals():
            signal.connect(self._signal_receiver, model)
        return model

    def get_adapter(self, model):
        """Returns the registration information for the given model class."""
        if self.is_registered(model):
            return self._registered_models[model]
        raise RegistrationError("{model} has not been registered with django-reversion".format(
            model=model,
        ))

    def unregister(self, model):
        """Removes a model from version control."""
        if not self.is_registered(model):
            raise RegistrationError("{model} has not been registered with django-reversion".format(
                model=model,
            ))
        adapter_obj = self._registered_models.pop(model)
        # Connect to the selected signals of the model.
        for signal in adapter_obj.get_all_signals():
            signal.disconnect(self._signal_receiver, model)

    def _get_versions(self, db=None):
        """Returns all versions that apply to this manager."""
        from reversion.models import Version
        return Version.objects.using(db).for_revision_manager(self)

    # Revision management API.

    def get_for_object_reference(self, model, object_id, db=None):
        """
        Returns all versions for the given object reference.

        The results are returned with the most recent versions first.
        """
        content_type = ContentType.objects.db_manager(db).get_for_model(model)
        versions = self._get_versions(db).filter(
            content_type=content_type,
            object_id=object_id,
        ).select_related("revision").order_by("-pk")
        return versions

    def get_for_object(self, obj, db=None):
        """
        Returns all the versions of the given object, ordered by date created.

        The results are returned with the most recent versions first.
        """
        return self.get_for_object_reference(obj.__class__, obj.pk, db)

    def get_unique_for_object(self, obj, db=None):
        """
        Returns unique versions associated with the object.

        The results are returned with the most recent versions first.
        """
        warnings.warn(
            "Use get_for_object().get_unique() instead of get_unique_for_object().",
            PendingDeprecationWarning)
        return list(self.get_for_object(obj, db).get_unique())

    def get_for_date(self, object, date, db=None):
        """Returns the latest version of an object for the given date."""
        return (self.get_for_object(object, db)
                .filter(revision__date_created__lte=date)[:1]
                .get())

    def get_deleted(self, model_class, db=None, model_db=None):
        """
        Returns all the deleted versions for the given model class.

        The results are returned with the most recent versions first.
        """
        model_db = model_db or db
        content_type = ContentType.objects.db_manager(db).get_for_model(model_class)
        # Return the deleted versions!
        return self._get_versions(db).filter(
            pk__reversion_in=(self._get_versions(db).filter(
                content_type=content_type,
            ).exclude(
                object_id__reversion_in=(model_class._base_manager.using(model_db), model_class._meta.pk.name),
            ).values_list("object_id").annotate(
                id=Max("id"),
            ), "id")
        ).order_by("-id")

    # Serialization.

    def _follow_relationships(self, instance):
        def follow(obj):
            # If a model is created an deleted in the same revision, then it's pk will be none.
            if obj.pk is None or obj in followed_objects:
                return
            followed_objects.add(obj)
            adapter = self.get_adapter(obj.__class__)
            for related in adapter.get_followed_relations(obj):
                follow(related)
        followed_objects = set()
        follow(instance)
        return followed_objects

    # Signal receivers.

    def _signal_receiver(self, instance, signal, **kwargs):
        """Adds registered models to the current revision, if any."""
        if self._revision_context_manager.is_active() and not self._revision_context_manager.is_managing_manually():
            adapter = self.get_adapter(instance.__class__)
            if signal in adapter.eager_signals:
                self._revision_context_manager.add_to_context_eager(self, instance)
            else:
                self._revision_context_manager.add_to_context(self, instance)


# A shared revision manager.
default_revision_manager = RevisionManager("default")


# Easy registration methods.
register = default_revision_manager.register
is_registered = default_revision_manager.is_registered
unregister = default_revision_manager.unregister
get_adapter = default_revision_manager.get_adapter
get_registered_models = default_revision_manager.get_registered_models


# Context management.
create_revision = revision_context_manager.create_revision


# Revision meta data.
get_user = revision_context_manager.get_user
set_user = revision_context_manager.set_user
get_comment = revision_context_manager.get_comment
set_comment = revision_context_manager.set_comment
add_meta = revision_context_manager.add_meta
get_ignore_duplicates = revision_context_manager.get_ignore_duplicates
set_ignore_duplicates = revision_context_manager.set_ignore_duplicates


# Low level API.
get_for_object_reference = default_revision_manager.get_for_object_reference
get_for_object = default_revision_manager.get_for_object
get_unique_for_object = default_revision_manager.get_unique_for_object
get_for_date = default_revision_manager.get_for_date
get_deleted = default_revision_manager.get_deleted
