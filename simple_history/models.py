from __future__ import unicode_literals

import copy
import importlib
import threading

from django.conf import settings
from django.contrib import admin
from django.db import models, router
from django.db.models.fields.proxy import OrderWrt
from django.utils import six
from django.utils.encoding import python_2_unicode_compatible, smart_text
from django.utils.timezone import now
from django.utils.translation import string_concat, ugettext_lazy as _

from . import exceptions, register
from .manager import HistoryDescriptor

try:
    from django.apps import apps
except ImportError:  # Django < 1.7
    from django.db.models import get_app
try:
    from south.modelsinspector import add_introspection_rules
except ImportError:  # south not present
    pass
else:  # south configuration for CustomForeignKeyField
    add_introspection_rules(
        [], ["^simple_history.models.CustomForeignKeyField"])

registered_models = {}
pending_registration = []


class HistoricalRecords(object):
    thread = threading.local()

    def __init__(self, verbose_name=None, bases=(models.Model,),
                 user_related_name='+', table_name=None, inherit=False,
                 excluded_fields=None, m2m_fields=None):
        self.user_set_verbose_name = verbose_name
        self.user_related_name = user_related_name
        self.table_name = table_name
        self.inherit = inherit
        self.m2m_fields = m2m_fields
        if excluded_fields is None:
            excluded_fields = []
        self.excluded_fields = excluded_fields
        try:
            if isinstance(bases, six.string_types):
                raise TypeError
            self.bases = tuple(bases)
        except TypeError:
            raise TypeError("The `bases` option must be a list or a tuple.")

    def contribute_to_class(self, cls, name):
        self.manager_name = name
        self.module = cls.__module__
        self.cls = cls
        models.signals.class_prepared.connect(self.finalize, weak=False)
        self.add_extra_methods(cls)

    def setup_m2m_history(self, cls):
        m2m_history_fields = self.m2m_fields
        if m2m_history_fields:
            assert isinstance(m2m_history_fields, list) or isinstance(m2m_history_fields, tuple), \
                'm2m_history_fields must be a list or tuple'
            for field_name in m2m_history_fields:
                field = getattr(cls, field_name).field
                assert isinstance(field, models.fields.related.ManyToManyField), \
                    '%s must be a ManyToManyField' % field_name

                if isinstance(field.rel.through, str):
                    model_path = field.rel.through.lower().split('.')
                    if len(model_path) == 2:
                        app_label, model_name = model_path
                    else:
                        app_label = cls._meta.app_label
                        model_name = model_path[0]

                    pending_registration.append((app_label, model_name))
                else:
                    self._setup_history(field.rel.through)

    def _setup_history(self, cls):
        if not sum([isinstance(item, HistoricalRecords) for item in cls.__dict__.values()]):
            cls.history = HistoricalRecords()
            register(cls)

    def m2m_changed(self, action, instance, sender, **kwargs):
        source_field_name, target_field_name = None, None
        for field_name, field_value in sender.__dict__.items():
            if isinstance(field_value, models.fields.related.ReverseSingleRelatedObjectDescriptor):
                try:
                    root_model = field_value.field.related.parent_model
                except AttributeError:
                    root_model = field_value.field.related.model

                if root_model == kwargs['model']:
                    target_field_name = field_name
                elif root_model == type(instance):
                    source_field_name = field_name

        items = sender.objects.filter(**{source_field_name: instance})
        if kwargs['pk_set']:
            items = items.filter(**{target_field_name + '__id__in': kwargs['pk_set']})
        for item in items:
            if action == 'post_add':
                if hasattr(item, 'skip_history_when_saving'):
                    return
                self.create_historical_record(item, '+')
            elif action == 'pre_remove':
                self.create_historical_record(item, '-')
            elif action == 'pre_clear':
                self.create_historical_record(item, '-')

    def add_extra_methods(self, cls):
        def save_without_historical_record(self, *args, **kwargs):
            """
            Save model without saving a historical record

            Make sure you know what you're doing before you use this method.
            """
            self.skip_history_when_saving = True
            try:
                ret = self.save(*args, **kwargs)
            finally:
                del self.skip_history_when_saving
            return ret

        setattr(cls, 'save_without_historical_record',
                save_without_historical_record)

        def save_as_draft(instance):
            self.create_historical_record(instance, '#')

        setattr(cls, 'save_as_draft', save_as_draft)

    def finalize(self, sender, **kwargs):
        if issubclass(sender, models.Model):
            key = (sender._meta.app_label, sender._meta.model_name)
            if key in pending_registration:
                pending_registration.remove(key)
                self._setup_history(sender)

        try:
            hint_class = self.cls
        except AttributeError:  # called via `register`
            pass
        else:
            if hint_class is not sender:  # set in concrete
                if not (self.inherit and issubclass(sender, hint_class)):
                    return  # set in abstract
        if hasattr(sender._meta, 'simple_history_manager_attribute'):
            raise exceptions.MultipleRegistrationsError(
                '{}.{} registered multiple times for history tracking.'.format(
                    sender._meta.app_label,
                    sender._meta.object_name,
                )
            )

        self.setup_m2m_history(sender)
        history_model = self.create_history_model(sender)
        module = importlib.import_module(self.module)
        setattr(module, history_model.__name__, history_model)

        # The HistoricalRecords object will be discarded,
        # so the signal handlers can't use weak references.
        models.signals.post_save.connect(self.post_save, sender=sender,
                                         weak=False)
        models.signals.m2m_changed.connect(self.m2m_changed, sender=sender, weak=False)

        models.signals.post_delete.connect(self.post_delete, sender=sender,
                                           weak=False)

        descriptor = HistoryDescriptor(history_model)
        setattr(sender, self.manager_name, descriptor)
        sender._meta.simple_history_manager_attribute = self.manager_name

    def create_history_model(self, model):
        """
        Creates a historical model to associate with the model provided.
        """
        attrs = {
            '__module__': self.module,
            'excluded_fields': self.excluded_fields
        }

        app_module = '%s.models' % model._meta.app_label
        if model.__module__ != self.module:
            # registered under different app
            attrs['__module__'] = self.module
        elif app_module != self.module:
            try:
                # Abuse an internal API because the app registry is loading.
                app = apps.app_configs[model._meta.app_label]
            except NameError:  # Django < 1.7
                models_module = get_app(model._meta.app_label).__name__
            else:
                models_module = app.name
            attrs['__module__'] = models_module

        fields = self.copy_fields(model)
        attrs.update(fields)
        attrs.update(self.get_extra_fields(model, fields))
        # type in python2 wants str as a first argument
        attrs.update(Meta=type(str('Meta'), (), self.get_meta_options(model)))
        if self.table_name is not None:
            attrs['Meta'].db_table = self.table_name
        name = 'Historical%s' % model._meta.object_name
        registered_models[model._meta.db_table] = model
        return python_2_unicode_compatible(
            type(str(name), self.bases, attrs))

    def fields_included(self, model):
        fields = []
        for field in model._meta.fields:
            if field.name not in self.excluded_fields:
                fields.append(field)
        return fields

    def copy_fields(self, model):
        """
        Creates copies of the model's original fields, returning
        a dictionary mapping field name to copied field object.
        """
        fields = {}
        for field in self.fields_included(model):
            field = copy.copy(field)
            try:
                field.remote_field = copy.copy(field.remote_field)
            except AttributeError:
                field.rel = copy.copy(field.rel)
            if isinstance(field, OrderWrt):
                # OrderWrt is a proxy field, switch to a plain IntegerField
                field.__class__ = models.IntegerField
            if isinstance(field, models.ForeignKey):
                old_field = field
                field_arguments = {'db_constraint': False}
                if (getattr(old_field, 'one_to_one', False) or
                        isinstance(old_field, models.OneToOneField)):
                    FieldType = models.ForeignKey
                else:
                    FieldType = type(old_field)
                if getattr(old_field, 'to_fields', []):
                    field_arguments['to_field'] = old_field.to_fields[0]
                if getattr(old_field, 'db_column', None):
                    field_arguments['db_column'] = old_field.db_column

                # If old_field.rel.to is 'self' then we have a case where object has a foreign key
                # to itself. In this case we update need to set the `to` value of the field
                # to be set to a model. We can use the old_field.model value.
                if isinstance(old_field.rel.to, str) and old_field.rel.to == 'self':
                    object_to = old_field.model
                else:
                    object_to = old_field.rel.to

                field = FieldType(
                    object_to,
                    related_name='+',
                    null=True,
                    blank=True,
                    primary_key=False,
                    db_index=True,
                    serialize=True,
                    unique=False,
                    on_delete=models.DO_NOTHING,
                    **field_arguments
                )
                field.name = old_field.name
            else:
                transform_field(field)
            fields[field.name] = field
        return fields

    def get_extra_fields(self, model, fields):
        """Return dict of extra fields added to the historical record model"""

        user_model = getattr(settings, 'AUTH_USER_MODEL', 'auth.User')

        @models.permalink
        def revert_url(self):
            """URL for this change in the default admin site."""
            opts = model._meta
            app_label, model_name = opts.app_label, opts.model_name
            return ('%s:%s_%s_simple_history' %
                    (admin.site.name, app_label, model_name),
                    [getattr(self, opts.pk.attname), self.history_id])

        def get_instance(self):
            attrs = {
                field.attname: getattr(self, field.attname)
                for field in fields.values()
            }
            if self.excluded_fields:
                excluded_attnames = [
                    model._meta.get_field(field).attname
                    for field in self.excluded_fields
                ]
                values = model.objects.filter(
                    pk=getattr(self, model._meta.pk.attname)
                ).values(*excluded_attnames).get()
                attrs.update(values)
            return model(**attrs)

        return {
            'history_id': models.AutoField(primary_key=True),
            'history_date': models.DateTimeField(),
            'history_change_reason': models.CharField(max_length=100,
                                                      null=True),
            'history_user': models.ForeignKey(
                user_model, null=True, related_name=self.user_related_name,
                on_delete=models.SET_NULL),
            'history_type': models.CharField(max_length=1, choices=(
                ('+', _('Created')),
                ('~', _('Changed')),
                ('-', _('Deleted')),
                ('#', _('Drafted')),
            )),
            'history_object': HistoricalObjectDescriptor(model, self.fields_included(model)),
            'instance': property(get_instance),
            'instance_type': model,
            'revert_url': revert_url,
            '__str__': lambda self: '%s as of %s' % (self.history_object,
                                                     self.history_date)
        }

    def get_meta_options(self, model):
        """
        Returns a dictionary of fields that will be added to
        the Meta inner class of the historical record model.
        """
        meta_fields = {
            'ordering': ('-history_date', '-history_id'),
            'get_latest_by': 'history_date',
        }
        if self.user_set_verbose_name:
            name = self.user_set_verbose_name
        else:
            name = string_concat('historical ',
                                 smart_text(model._meta.verbose_name))
        meta_fields['verbose_name'] = name
        return meta_fields

    def post_save(self, instance, created, **kwargs):
        if not created and hasattr(instance, 'skip_history_when_saving'):
            return
        if not kwargs.get('raw', False):
            self.create_historical_record(instance, created and '+' or '~')

    def post_delete(self, instance, **kwargs):
        self.create_historical_record(instance, '-')

    def create_historical_record(self, instance, history_type):
        history_date = getattr(instance, '_history_date', now())
        history_user = self.get_history_user(instance)
        history_change_reason = getattr(instance, 'changeReason', None)
        manager = getattr(instance, self.manager_name)
        attrs = {}
        for field in self.fields_included(instance):
            attrs[field.attname] = getattr(instance, field.attname)
        manager.create(history_date=history_date, history_type=history_type,
                       history_user=history_user,
                       history_change_reason=history_change_reason, **attrs)

    def get_history_user(self, instance):
        """Get the modifying user from instance or middleware."""
        try:
            return instance._history_user
        except AttributeError:
            try:
                if self.thread.request.user.is_authenticated():
                    return self.thread.request.user
                return None
            except AttributeError:
                return None


def transform_field(field):
    """Customize field appropriately for use in historical model"""
    field.name = field.attname
    if isinstance(field, models.AutoField):
        field.__class__ = convert_auto_field(field)

    elif isinstance(field, models.FileField):
        # Don't copy file, just path.
        field.__class__ = models.TextField

    # Historical instance shouldn't change create/update timestamps
    field.auto_now = False
    field.auto_now_add = False

    if field.primary_key or field.unique:
        # Unique fields can no longer be guaranteed unique,
        # but they should still be indexed for faster lookups.
        field.primary_key = False
        field._unique = False
        field.db_index = True
        field.serialize = True


def convert_auto_field(field):
    """Convert AutoField to a non-incrementing type

    The historical model gets its own AutoField, so any existing one
    must be replaced with an IntegerField.
    """
    connection = router.db_for_write(field.model)
    if settings.DATABASES[connection].get('ENGINE') in ('django_mongodb_engine',):
        # Check if AutoField is string for django-non-rel support
        return models.TextField
    return models.IntegerField


class HistoricalObjectDescriptor(object):
    def __init__(self, model, fields_included):
        self.model = model
        self.fields_included = fields_included

    def __get__(self, instance, owner):
        values = (getattr(instance, f.attname)
                  for f in self.fields_included)
        return self.model(*values)
