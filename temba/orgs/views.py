import itertools
import logging
import smtplib
from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parseaddr
from functools import cmp_to_key
from urllib.parse import parse_qs, unquote, urlparse

import pyotp
import pytz
import requests
from packaging.version import Version
from smartmin.users.views import Login
from smartmin.views import (
    SmartCreateView,
    SmartCRUDL,
    SmartDeleteView,
    SmartFormView,
    SmartListView,
    SmartModelActionView,
    SmartModelFormView,
    SmartReadView,
    SmartTemplateView,
    SmartUpdateView,
)
from twilio.rest import Client

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import LoginView as AuthLoginView
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.db.models import ExpressionWrapper, F, IntegerField, Q, Sum
from django.forms import Form
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.encoding import DjangoUnicodeDecodeError, force_text
from django.utils.html import escape
from django.utils.http import urlquote
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from temba.api.models import APIToken
from temba.campaigns.models import Campaign
from temba.channels.models import Channel
from temba.contacts.models import ContactGroupCount
from temba.flows.models import Flow
from temba.formax import FormaxMixin
from temba.utils import analytics, get_anonymous_user, json, languages, str_to_bool
from temba.utils.email import is_valid_address
from temba.utils.fields import (
    ArbitraryJsonChoiceField,
    CheckboxWidget,
    InputWidget,
    SelectMultipleWidget,
    SelectWidget,
)
from temba.utils.http import http_headers
from temba.utils.timezones import TimeZoneFormField
from temba.utils.views import ComponentFormMixin, NonAtomicMixin

from .models import BackupToken, Invitation, Org, OrgCache, OrgRole, TopUp, get_stripe_credentials
from .tasks import apply_topups_task

# session key for storing a two-factor enabled user's id once we've checked their password
TWO_FACTOR_USER_SESSION_KEY = "_two_factor_user_id"


def check_login(request):
    """
    Simple view that checks whether we actually need to log in.  This is needed on the live site
    because we serve the main page as http:// but the logged in pages as https:// and only store
    the cookies on the SSL connection.  This view will be called in https:// land where we will
    check whether we are logged in, if so then we will redirect to the LOGIN_URL, otherwise we take
    them to the normal user login page
    """
    if request.user.is_authenticated:
        return HttpResponseRedirect(settings.LOGIN_REDIRECT_URL)
    else:
        return HttpResponseRedirect(settings.LOGIN_URL)


class OrgPermsMixin(object):
    """
    Get the organisation and the user within the inheriting view so that it be come easy to decide
    whether this user has a certain permission for that particular organization to perform the view's actions
    """

    def get_user(self):
        return self.request.user

    def derive_org(self):
        org = None
        if not self.get_user().is_anonymous:
            org = self.get_user().get_org()
        return org

    def has_org_perm(self, permission):
        if self.org:
            return self.get_user().has_org_perm(self.org, permission)
        return False

    def has_permission(self, request, *args, **kwargs):
        """
        Figures out if the current user has permissions for this view.
        """
        self.kwargs = kwargs
        self.args = args
        self.request = request
        self.org = self.derive_org()

        if self.get_user().is_superuser:
            return True

        if self.get_user().is_anonymous:
            return False

        if self.get_user().has_perm(self.permission):  # pragma: needs cover
            return True

        return self.has_org_perm(self.permission)

    def dispatch(self, request, *args, **kwargs):

        # non admin authenticated users without orgs get the org chooser
        user = self.get_user()
        if user.is_authenticated and not (user.is_superuser or user.is_staff):
            if not self.derive_org():
                return HttpResponseRedirect(reverse("orgs.org_choose"))

        return super().dispatch(request, *args, **kwargs)


class AnonMixin(OrgPermsMixin):
    """
    Mixin that makes sure that anonymous orgs cannot add channels (have no permission if anon)
    """

    def has_permission(self, request, *args, **kwargs):
        org = self.derive_org()

        # can this user break anonymity? then we are fine
        if self.get_user().has_perm("contacts.contact_break_anon"):
            return True

        # otherwise if this org is anon, no go
        if not org or org.is_anon:
            return False
        else:
            return super().has_permission(request, *args, **kwargs)


class OrgObjPermsMixin(OrgPermsMixin):
    def get_object_org(self):
        return self.get_object().org

    def has_org_perm(self, codename):
        has_org_perm = super().has_org_perm(codename)

        if has_org_perm:
            user = self.get_user()
            return user.get_org() == self.get_object_org()

        return False

    def has_permission(self, request, *args, **kwargs):
        has_perm = super().has_permission(request, *args, **kwargs)

        if has_perm:
            user = self.get_user()

            # user has global permission
            if user.has_perm(self.permission):
                return True

            return user.get_org() == self.get_object_org()

        return False


class ModalMixin(SmartFormView):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if "HTTP_X_PJAX" in self.request.META and "HTTP_X_FORMAX" not in self.request.META:  # pragma: no cover
            context["base_template"] = "smartmin/modal.html"
        if "success_url" in kwargs:  # pragma: no cover
            context["success_url"] = kwargs["success_url"]

        pairs = [urlquote(k) + "=" + urlquote(v) for k, v in self.request.GET.items() if k != "_"]
        context["action_url"] = self.request.path + "?" + ("&".join(pairs))

        return context

    def form_valid(self, form):
        if isinstance(form, forms.ModelForm):
            self.object = form.save(commit=False)

        try:
            if isinstance(self, SmartModelFormView):
                self.object = self.pre_save(self.object)
                self.save(self.object)
                self.object = self.post_save(self.object)

            elif isinstance(self, SmartModelActionView):
                self.execute_action()

            messages.success(self.request, self.derive_success_message())

            if "HTTP_X_PJAX" not in self.request.META:
                return HttpResponseRedirect(self.get_success_url())
            else:  # pragma: no cover
                response = self.render_to_response(
                    self.get_context_data(
                        form=form,
                        success_url=self.get_success_url(),
                        success_script=getattr(self, "success_script", None),
                    )
                )
                response["Temba-Success"] = self.get_success_url()
                return response

        except (IntegrityError, ValueError, ValidationError) as e:
            message = getattr(e, "message", str(e).capitalize())
            self.form.add_error(None, message)
            return self.render_to_response(self.get_context_data(form=form))


class OrgSignupForm(forms.ModelForm):
    """
    Signup for new organizations
    """

    first_name = forms.CharField(
        max_length=User._meta.get_field("first_name").max_length,
        widget=InputWidget(attrs={"widget_only": True, "placeholder": _("First name")}),
    )
    last_name = forms.CharField(
        max_length=User._meta.get_field("last_name").max_length,
        widget=InputWidget(attrs={"widget_only": True, "placeholder": _("Last name")}),
    )
    email = forms.EmailField(
        max_length=User._meta.get_field("username").max_length,
        widget=InputWidget(attrs={"widget_only": True, "placeholder": _("name@domain.com")}),
    )

    timezone = TimeZoneFormField(help_text=_("The timezone for your workspace"), widget=forms.widgets.HiddenInput())

    password = forms.CharField(
        widget=InputWidget(attrs={"hide_label": True, "password": True, "placeholder": _("Password")}),
        validators=[validate_password],
        help_text=_("At least eight characters or more"),
    )

    name = forms.CharField(
        label=_("Workspace"),
        help_text=_("A workspace is usually the name of a company or project"),
        widget=InputWidget(attrs={"widget_only": False, "placeholder": _("My Company, Inc.")}),
    )

    def __init__(self, *args, **kwargs):
        if "branding" in kwargs:
            del kwargs["branding"]

        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email:
            if User.objects.filter(username__iexact=email):
                raise forms.ValidationError(_("That email address is already used"))

        return email.lower()

    class Meta:
        model = Org
        fields = "__all__"


class OrgGrantForm(forms.ModelForm):
    first_name = forms.CharField(
        help_text=_("The first name of the workspace administrator"),
        max_length=User._meta.get_field("first_name").max_length,
    )
    last_name = forms.CharField(
        help_text=_("Your last name of the workspace administrator"),
        max_length=User._meta.get_field("last_name").max_length,
    )
    email = forms.EmailField(
        help_text=_("Their email address"), max_length=User._meta.get_field("username").max_length
    )
    timezone = TimeZoneFormField(help_text=_("The timezone for the workspace"))
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
        help_text=_("Their password, at least eight letters please. (leave blank for existing login)"),
    )
    name = forms.CharField(label=_("Workspace"), help_text=_("The name of the new workspace"))
    credits = forms.ChoiceField(choices=(), help_text=_("The initial number of credits granted to this workspace"))

    def __init__(self, *args, **kwargs):
        branding = kwargs["branding"]
        del kwargs["branding"]

        super().__init__(*args, **kwargs)

        welcome_packs = branding["welcome_packs"]

        choices = []
        for pack in welcome_packs:
            choices.append((str(pack["size"]), "%d - %s" % (pack["size"], pack["name"])))

        self.fields["credits"].choices = choices

    def clean(self):
        data = self.cleaned_data

        email = data.get("email", None)
        password = data.get("password", None)

        # for granting new accounts, either the email maps to an existing user (and their existing password is used)
        # or both email and password must be included
        if email:
            user = User.objects.filter(username__iexact=email).first()
            if user:
                if password:
                    raise ValidationError(_("Login already exists, please do not include password."))
            else:
                if not password:
                    raise ValidationError(_("Password required for new login."))

                validate_password(password)

        return data

    class Meta:
        model = Org
        fields = "__all__"


class LoginView(Login):
    """
    Overrides the smartmin login view to redirect users with 2FA enabled to a second verification view.
    """

    template_name = "orgs/login/login.haml"

    def form_valid(self, form):
        user = form.get_user()
        if user.get_settings().two_factor_enabled:
            self.request.session[TWO_FACTOR_USER_SESSION_KEY] = str(user.id)

            verify_url = reverse("users.two_factor_verify")
            redirect_url = self.get_redirect_url()
            if redirect_url:
                verify_url += f"?{self.redirect_field_name}={urlquote(redirect_url)}"

            return HttpResponseRedirect(verify_url)

        return super().form_valid(form)


class BaseTwoFactorView(AuthLoginView):
    def dispatch(self, request, *args, **kwargs):
        # redirect back to login view if user hasn't completed that yet
        user = self.get_user()
        if not user:
            return HttpResponseRedirect(reverse("users.login"))

        return super().dispatch(request, *args, **kwargs)

    def get_user(self):
        user_id = self.request.session.get(TWO_FACTOR_USER_SESSION_KEY)
        if user_id:
            return User.objects.filter(id=user_id, is_active=True).first()
        return None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.get_user()
        return kwargs

    def form_valid(self, form):
        # set the user as actually authenticated now
        login(self.request, self.get_user())

        # remove our session key so if the user comes back this page they'll get directed to the login view
        self.request.session.pop(TWO_FACTOR_USER_SESSION_KEY, None)

        return HttpResponseRedirect(self.get_success_url())


class TwoFactorVerifyView(BaseTwoFactorView):
    """
    View to let users with 2FA enabled verify their identity via an OTP from a device.
    """

    class Form(forms.Form):
        otp = forms.CharField(max_length=6, required=True)

        def __init__(self, request, user, *args, **kwargs):
            self.user = user
            super().__init__(*args, **kwargs)

        def clean_otp(self):
            data = self.cleaned_data["otp"]
            if not self.user.verify_2fa(otp=data):
                raise ValidationError(_("Incorrect OTP. Please try again."))
            return data

    form_class = Form
    template_name = "orgs/login/two_factor_verify.haml"


class TwoFactorBackupView(BaseTwoFactorView):
    """
    View to let users with 2FA enabled verify their identity using a backup token.
    """

    class Form(forms.Form):
        token = forms.CharField(max_length=8, required=True)

        def __init__(self, request, user, *args, **kwargs):
            self.user = user
            super().__init__(*args, **kwargs)

        def clean_token(self):
            data = self.cleaned_data["token"]
            if not self.user.verify_2fa(backup_token=data):
                raise ValidationError(_("Invalid backup token. Please try again."))
            return data

    form_class = Form
    template_name = "orgs/login/two_factor_backup.haml"


class InferOrgMixin:
    @classmethod
    def derive_url_pattern(cls, path, action):
        return r"^%s/%s/$" % (path, action)

    def get_object(self, *args, **kwargs):
        return self.request.user.get_org()


class UserCRUDL(SmartCRUDL):
    model = User
    actions = ("list", "edit", "delete", "two_factor_enable", "two_factor_disable", "two_factor_tokens")

    class List(SmartListView):
        fields = ("username", "orgs", "date_joined")
        link_fields = ("username",)
        ordering = ("-date_joined",)
        search_fields = ("username",)

        def get_username(self, user):
            return mark_safe(f"<a href='{reverse('users.user_update', args=(user.id,))}'>{user.username}</a>")

        def get_orgs(self, user):
            orgs = user.get_user_orgs()[0:6]

            more = ""
            if len(orgs) > 5:
                more = ", ..."
                orgs = orgs[0:5]
            org_links = ", ".join(
                [f"<a href='{reverse('orgs.org_update', args=[org.id])}'>{escape(org.name)}</a>" for org in orgs]
            )
            return mark_safe(f"{org_links}{more}")

        def derive_queryset(self, **kwargs):
            return super().derive_queryset(**kwargs).filter(is_active=True).exclude(id=get_anonymous_user().id)

    class Delete(SmartUpdateView):
        class DeleteForm(forms.ModelForm):
            delete = forms.BooleanField()

            class Meta:
                model = User
                fields = ("delete",)

        form_class = DeleteForm
        permission = "auth.user_update"

        def form_valid(self, form):
            user = self.get_object()
            username = user.username

            brand = self.request.branding.get("brand")
            user.release(brand)

            messages.success(self.request, _(f"Deleted user {username}"))
            return HttpResponseRedirect(reverse("orgs.user_list", args=()))

    class Edit(SmartUpdateView):
        class EditForm(forms.ModelForm):
            first_name = forms.CharField(
                label=_("First Name"), widget=InputWidget(attrs={"placeholder": _("Required")})
            )
            last_name = forms.CharField(label=_("Last Name"), widget=InputWidget(attrs={"placeholder": _("Required")}))
            email = forms.EmailField(required=True, label=_("Email"), widget=InputWidget())
            current_password = forms.CharField(
                label=_("Current Password"), widget=InputWidget(attrs={"placeholder": _("Required"), "password": True})
            )
            new_password = forms.CharField(
                required=False,
                label=_("New Password"),
                widget=InputWidget(attrs={"placeholder": _("Optional"), "password": True}),
            )
            language = forms.ChoiceField(
                choices=settings.LANGUAGES, required=True, label=_("Website Language"), widget=SelectWidget()
            )

            def clean_new_password(self):
                password = self.cleaned_data["new_password"]
                if password and not len(password) >= 8:
                    raise forms.ValidationError(_("Passwords must have at least 8 letters."))
                return password

            def clean_current_password(self):
                user = self.instance
                password = self.cleaned_data.get("current_password", None)

                if not user.check_password(password):
                    raise forms.ValidationError(_("Please enter your password to save changes."))

                return password

            def clean_email(self):
                user = self.instance
                email = self.cleaned_data["email"].lower()

                if User.objects.filter(username=email).exclude(pk=user.pk):
                    raise forms.ValidationError(_("Sorry, that email address is already taken."))

                return email

            class Meta:
                model = User
                fields = ("first_name", "last_name", "email", "current_password", "new_password", "language")

        form_class = EditForm
        permission = "orgs.org_profile"
        success_url = "@orgs.org_home"
        success_message = ""

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r"^%s/%s/$" % (path, action)

        def get_object(self, *args, **kwargs):
            return self.request.user

        def derive_initial(self):
            initial = super().derive_initial()
            initial["language"] = self.get_object().get_settings().language
            return initial

        def pre_save(self, obj):
            obj = super().pre_save(obj)

            # keep our username and email in sync
            obj.username = obj.email

            if self.form.cleaned_data["new_password"]:
                obj.set_password(self.form.cleaned_data["new_password"])

            return obj

        def post_save(self, obj):
            # save the user settings as well
            obj = super().post_save(obj)
            user_settings = obj.get_settings()
            user_settings.language = self.form.cleaned_data["language"]
            user_settings.save()
            return obj

        def has_permission(self, request, *args, **kwargs):
            user = self.request.user

            if user.is_anonymous:
                return False

            org = user.get_org()

            if org:
                if not user.is_authenticated:  # pragma: needs cover
                    return False

                if org.has_user(user):
                    return True

            return False  # pragma: needs cover

    class TwoFactorEnable(ComponentFormMixin, InferOrgMixin, OrgPermsMixin, SmartFormView):
        class Form(forms.Form):
            otp = forms.CharField(
                label="The generated OTP",
                widget=InputWidget(attrs={"placeholder": _("6-digit code")}),
                max_length=6,
                required=True,
            )
            password = forms.CharField(
                label="Your current login password",
                widget=InputWidget(attrs={"placeholder": _("Current password"), "password": True}),
                required=True,
            )

            def __init__(self, user, *args, **kwargs):
                super().__init__(*args, **kwargs)

                self.user = user

            def clean_otp(self):
                data = self.cleaned_data["otp"]
                if not self.user.verify_2fa(otp=data):
                    raise forms.ValidationError(_("OTP incorrect. Please try again."))
                return data

            def clean_password(self):
                data = self.cleaned_data["password"]
                if not self.user.check_password(data):
                    raise forms.ValidationError(_("Password incorrect."))
                return data

        form_class = Form
        success_url = "@orgs.user_two_factor_tokens"
        success_message = _("Two-factor authentication enabled")
        submit_button_name = _("Enable")
        permission = "orgs.org_two_factor"
        title = _("Enable Two-factor Authentication")

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            brand = self.request.branding["name"]
            user = self.get_user()
            user_settings = user.get_settings()
            secret_url = pyotp.TOTP(user_settings.otp_secret).provisioning_uri(user.username, issuer_name=brand)
            context["secret_url"] = secret_url
            return context

        def form_valid(self, form):
            self.request.user.enable_2fa()

            return super().form_valid(form)

    class TwoFactorDisable(ComponentFormMixin, InferOrgMixin, OrgPermsMixin, SmartFormView):
        class Form(forms.Form):
            password = forms.CharField(
                label=" ",
                widget=InputWidget(attrs={"placeholder": _("Current password"), "password": True}),
                required=True,
            )

            def __init__(self, user, *args, **kwargs):
                super().__init__(*args, **kwargs)

                self.user = user

            def clean_password(self):
                data = self.cleaned_data["password"]
                if not self.user.check_password(data):
                    raise forms.ValidationError(_("Password incorrect."))
                return data

        form_class = Form
        success_url = "@orgs.org_home"
        success_message = _("Two-factor authentication disabled")
        submit_button_name = _("Disable")
        permission = "orgs.org_two_factor"
        title = _("Disable Two-factor Authentication")

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def form_valid(self, form):
            self.request.user.disable_2fa()

            return super().form_valid(form)

    class TwoFactorTokens(InferOrgMixin, OrgPermsMixin, SmartTemplateView):
        permission = "orgs.org_two_factor"
        title = _("Two-factor Authentication")

        def pre_process(self, request, *args, **kwargs):
            # if 2FA isn't enabled for this user, take them to the enable view instead
            if not self.request.user.get_settings().two_factor_enabled:
                return HttpResponseRedirect(reverse("orgs.user_two_factor_enable"))

            return super().pre_process(request, *args, **kwargs)

        def post(self, request, *args, **kwargs):
            BackupToken.generate_for_user(self.request.user)
            messages.info(request, _("Two-factor authentication backup tokens changed."))

            return super().get(request, *args, **kwargs)

        def get_gear_links(self):
            return [
                dict(title=_("Home"), style="button-light", href=reverse("orgs.org_home")),
                dict(title=_("Disable"), style="button-light", href=reverse("orgs.user_two_factor_disable")),
            ]

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["backup_tokens"] = self.get_user().backup_tokens.order_by("id")
            return context


class OrgCRUDL(SmartCRUDL):
    actions = (
        "signup",
        "home",
        "token",
        "edit",
        "edit_sub_org",
        "join",
        "grant",
        "accounts",
        "create_login",
        "chatbase",
        "choose",
        "delete",
        "manage_accounts",
        "manage_accounts_sub_org",
        "manage",
        "update",
        "country",
        "languages",
        "clear_cache",
        "twilio_connect",
        "twilio_account",
        "nexmo_account",
        "nexmo_connect",
        "plan",
        "sub_orgs",
        "create_sub_org",
        "export",
        "import",
        "plivo_connect",
        "prometheus",
        "resthooks",
        "service",
        "surveyor",
        "transfer_credits",
        "dtone_account",
        "smtp_server",
    )

    model = Org

    class Import(NonAtomicMixin, InferOrgMixin, OrgPermsMixin, SmartFormView):
        class FlowImportForm(Form):
            import_file = forms.FileField(help_text=_("The import file"))
            update = forms.BooleanField(help_text=_("Update all flows and campaigns"), required=False)

            def __init__(self, *args, **kwargs):
                self.org = kwargs["org"]
                del kwargs["org"]
                super().__init__(*args, **kwargs)

            def clean_import_file(self):
                # check that it isn't too old
                data = self.cleaned_data["import_file"].read()
                try:
                    json_data = json.loads(force_text(data))
                except (DjangoUnicodeDecodeError, ValueError):
                    raise ValidationError(_("This file is not a valid flow definition file."))

                if Version(str(json_data.get("version", 0))) < Version(Org.EARLIEST_IMPORT_VERSION):
                    raise ValidationError(
                        _("This file is no longer valid. Please export a new version and try again.")
                    )

                return data

        success_message = _("Import successful")
        form_class = FlowImportForm

        def get_success_url(self):  # pragma: needs cover
            return reverse("orgs.org_home")

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["org"] = self.request.user.get_org()
            return kwargs

        def form_valid(self, form):
            try:
                org = self.request.user.get_org()
                data = json.loads(form.cleaned_data["import_file"])
                org.import_app(data, self.request.user, self.request.branding["link"])
            except Exception as e:
                # this is an unexpected error, report it to sentry
                logger = logging.getLogger(__name__)
                logger.error("Exception on app import: %s" % str(e), exc_info=True)
                form._errors["import_file"] = form.error_class([_("Sorry, your import file is invalid.")])
                return self.form_invalid(form)

            return super().form_valid(form)  # pragma: needs cover

    class Export(InferOrgMixin, OrgPermsMixin, SmartTemplateView):
        def post(self, request, *args, **kwargs):
            org = self.get_object()

            flow_ids = [elt for elt in self.request.POST.getlist("flows") if elt]
            campaign_ids = [elt for elt in self.request.POST.getlist("campaigns") if elt]

            # fetch the selected flows and campaigns
            flows = Flow.objects.filter(id__in=flow_ids, org=org, is_active=True)
            campaigns = Campaign.objects.filter(id__in=campaign_ids, org=org, is_active=True)

            components = set(itertools.chain(flows, campaigns))

            # add triggers for the selected flows
            for flow in flows:
                components.update(flow.triggers.filter(is_active=True, is_archived=False))

            export = org.export_definitions(request.branding["link"], components)
            response = JsonResponse(export, json_dumps_params=dict(indent=2))
            response["Content-Disposition"] = "attachment; filename=%s.json" % slugify(org.name)
            return response

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            org = self.get_object()
            include_archived = bool(int(self.request.GET.get("archived", 0)))

            buckets, singles = self.generate_export_buckets(org, include_archived)

            context["archived"] = include_archived
            context["buckets"] = buckets
            context["singles"] = singles

            context["flow_id"] = int(self.request.GET.get("flow", 0))
            context["campaign_id"] = int(self.request.GET.get("campaign", 0))

            return context

        def generate_export_buckets(self, org, include_archived):
            """
            Generates a set of buckets of related exportable flows and campaigns
            """
            dependencies = org.generate_dependency_graph(include_archived=include_archived)

            unbucketed = set(dependencies.keys())
            buckets = []

            # helper method to add a component and its dependencies to a bucket
            def collect_component(c, bucket):
                if c in bucket:  # pragma: no cover
                    return

                unbucketed.remove(c)
                bucket.add(c)

                for d in dependencies[c]:
                    if d in unbucketed:
                        collect_component(d, bucket)

            while unbucketed:
                component = next(iter(unbucketed))

                bucket = set()
                buckets.append(bucket)

                collect_component(component, bucket)

            # collections with only one non-group component should be merged into a single "everything else" collection
            non_single_buckets = []
            singles = set()

            # items within buckets are sorted by type and name
            def sort_key(c):
                return c.__class__.__name__, c.name.lower()

            # buckets with a single item are merged into a special singles bucket
            for b in buckets:
                if len(b) > 1:
                    sorted_bucket = sorted(list(b), key=sort_key)
                    non_single_buckets.append(sorted_bucket)
                else:
                    singles.update(b)

            # put the buckets with the most items first
            non_single_buckets = sorted(non_single_buckets, key=lambda b: len(b), reverse=True)

            # sort singles
            singles = sorted(list(singles), key=sort_key)

            return non_single_buckets, singles

    class TwilioConnect(ComponentFormMixin, ModalMixin, InferOrgMixin, OrgPermsMixin, SmartFormView):
        class TwilioConnectForm(forms.Form):
            account_sid = forms.CharField(help_text=_("Your Twilio Account SID"), widget=InputWidget())
            account_token = forms.CharField(help_text=_("Your Twilio Account Token"), widget=InputWidget())

            def clean(self):
                account_sid = self.cleaned_data.get("account_sid", None)
                account_token = self.cleaned_data.get("account_token", None)

                if not account_sid:  # pragma: needs cover
                    raise ValidationError(_("You must enter your Twilio Account SID"))

                if not account_token:
                    raise ValidationError(_("You must enter your Twilio Account Token"))

                try:
                    client = Client(account_sid, account_token)

                    # get the actual primary auth tokens from twilio and use them
                    account = client.api.account.fetch()
                    self.cleaned_data["account_sid"] = account.sid
                    self.cleaned_data["account_token"] = account.auth_token
                except Exception:
                    raise ValidationError(
                        _("The Twilio account SID and Token seem invalid. Please check them again and retry.")
                    )

                return self.cleaned_data

        form_class = TwilioConnectForm
        submit_button_name = "Save"
        field_config = dict(account_sid=dict(label=""), account_token=dict(label=""))
        success_message = "Twilio Account successfully connected."

        def get_success_url(self):
            claim_type = self.request.GET.get("claim_type", "twilio")

            if claim_type == "twilio_messaging_service":
                return reverse("channels.types.twilio_messaging_service.claim")

            if claim_type == "twilio_whatsapp":
                return reverse("channels.types.twilio_whatsapp.claim")

            if claim_type == "twilio":
                return reverse("channels.types.twilio.claim")

            return reverse("channels.channel_claim")

        def form_valid(self, form):
            account_sid = form.cleaned_data["account_sid"]
            account_token = form.cleaned_data["account_token"]

            org = self.get_object()
            org.connect_twilio(account_sid, account_token, self.request.user)
            org.save()

            return HttpResponseRedirect(self.get_success_url())

    class NexmoAccount(InferOrgMixin, ComponentFormMixin, OrgPermsMixin, SmartUpdateView):
        success_message = ""

        class NexmoKeys(forms.ModelForm):
            api_key = forms.CharField(max_length=128, label=_("API Key"), required=False)
            api_secret = forms.CharField(max_length=128, label=_("API Secret"), required=False)
            disconnect = forms.CharField(widget=forms.HiddenInput, max_length=6, required=True)

            def clean(self):
                super().clean()
                if self.cleaned_data.get("disconnect", "false") == "false":
                    api_key = self.cleaned_data.get("api_key", None)
                    api_secret = self.cleaned_data.get("api_secret", None)

                    if not api_key:
                        raise ValidationError(_("You must enter your Nexmo Account API Key"))

                    if not api_secret:  # pragma: needs cover
                        raise ValidationError(_("You must enter your Nexmo Account API Secret"))

                    from temba.channels.types.nexmo.client import NexmoClient

                    if not NexmoClient(api_key, api_secret).check_credentials():
                        raise ValidationError(
                            _("Your Nexmo API key and secret seem invalid. Please check them again and retry.")
                        )

                return self.cleaned_data

            class Meta:
                model = Org
                fields = ("api_key", "api_secret", "disconnect")

        form_class = NexmoKeys

        def derive_initial(self):
            initial = super().derive_initial()
            org = self.get_object()
            config = org.config
            initial["api_key"] = config.get(Org.CONFIG_NEXMO_KEY, "")
            initial["api_secret"] = config.get(Org.CONFIG_NEXMO_SECRET, "")
            initial["disconnect"] = "false"
            return initial

        def form_valid(self, form):
            disconnect = form.cleaned_data.get("disconnect", "false") == "true"
            user = self.request.user
            org = user.get_org()

            if disconnect:
                org.remove_nexmo_account(user)
                return HttpResponseRedirect(reverse("orgs.org_home"))
            else:
                api_key = form.cleaned_data["api_key"]
                api_secret = form.cleaned_data["api_secret"]

                org.connect_nexmo(api_key, api_secret, user)
                return super().form_valid(form)

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            org = self.get_object()
            client = org.get_nexmo_client()
            if client:
                config = org.config
                context["api_key"] = config.get(Org.CONFIG_NEXMO_KEY, "--")

            return context

    class NexmoConnect(ModalMixin, InferOrgMixin, OrgPermsMixin, SmartFormView):
        class NexmoConnectForm(forms.Form):
            api_key = forms.CharField(help_text=_("Your Nexmo API key"), widget=InputWidget())
            api_secret = forms.CharField(help_text=_("Your Nexmo API secret"), widget=InputWidget())

            def clean(self):
                super().clean()

                api_key = self.cleaned_data.get("api_key")
                api_secret = self.cleaned_data.get("api_secret")

                from temba.channels.types.nexmo.client import NexmoClient

                if not NexmoClient(api_key, api_secret).check_credentials():
                    raise ValidationError(
                        _("Your Nexmo API key and secret seem invalid. Please check them again and retry.")
                    )

                return self.cleaned_data

        form_class = NexmoConnectForm
        submit_button_name = "Save"
        success_url = "@channels.types.nexmo.claim"
        field_config = dict(api_key=dict(label=""), api_secret=dict(label=""))
        success_message = "Nexmo Account successfully connected."

        def form_valid(self, form):
            api_key = form.cleaned_data["api_key"]
            api_secret = form.cleaned_data["api_secret"]

            org = self.get_object()

            org.connect_nexmo(api_key, api_secret, self.request.user)

            org.save()

            return HttpResponseRedirect(self.get_success_url())

    class Plan(InferOrgMixin, OrgPermsMixin, SmartReadView):
        pass

    class PlivoConnect(ModalMixin, ComponentFormMixin, InferOrgMixin, OrgPermsMixin, SmartFormView):
        class PlivoConnectForm(forms.Form):
            auth_id = forms.CharField(help_text=_("Your Plivo AUTH ID"))
            auth_token = forms.CharField(help_text=_("Your Plivo AUTH TOKEN"))

            def clean(self):
                super().clean()

                auth_id = self.cleaned_data.get("auth_id", None)
                auth_token = self.cleaned_data.get("auth_token", None)

                headers = http_headers(extra={"Content-Type": "application/json"})

                response = requests.get(
                    "https://api.plivo.com/v1/Account/%s/" % auth_id, headers=headers, auth=(auth_id, auth_token)
                )

                if response.status_code != 200:
                    raise ValidationError(
                        _("Your Plivo AUTH ID and AUTH TOKEN seem invalid. Please check them again and retry.")
                    )

                return self.cleaned_data

        form_class = PlivoConnectForm
        submit_button_name = "Save"
        success_url = "@channels.types.plivo.claim"
        field_config = dict(auth_id=dict(label=""), auth_token=dict(label=""))
        success_message = "Plivo credentials verified. You can now add a Plivo channel."

        def form_valid(self, form):

            auth_id = form.cleaned_data["auth_id"]
            auth_token = form.cleaned_data["auth_token"]

            # add the credentials to the session
            self.request.session[Channel.CONFIG_PLIVO_AUTH_ID] = auth_id
            self.request.session[Channel.CONFIG_PLIVO_AUTH_TOKEN] = auth_token

            return HttpResponseRedirect(self.get_success_url())

    class SmtpServer(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        success_message = ""

        class SmtpConfig(forms.ModelForm):
            smtp_from_email = forms.CharField(
                max_length=128,
                label=_("Email Address"),
                required=False,
                help_text=_("The from email address, can contain a name: ex: Jane Doe <jane@example.org>"),
                widget=InputWidget(),
            )
            smtp_host = forms.CharField(
                max_length=128,
                required=False,
                widget=InputWidget(attrs={"widget_only": True, "placeholder": _("SMTP Host")}),
            )
            smtp_username = forms.CharField(max_length=128, label=_("Username"), required=False, widget=InputWidget())
            smtp_password = forms.CharField(
                max_length=128,
                label=_("Password"),
                required=False,
                help_text=_("Leave blank to keep the existing set password if one exists"),
                widget=InputWidget(attrs={"password": True}),
            )
            smtp_port = forms.CharField(
                max_length=128,
                required=False,
                widget=InputWidget(attrs={"widget_only": True, "placeholder": _("Port")}),
            )
            disconnect = forms.CharField(widget=forms.HiddenInput, max_length=6, required=True)

            def clean(self):
                super().clean()
                if self.cleaned_data.get("disconnect", "false") == "false":
                    smtp_from_email = self.cleaned_data.get("smtp_from_email", None)
                    smtp_host = self.cleaned_data.get("smtp_host", None)
                    smtp_username = self.cleaned_data.get("smtp_username", None)
                    smtp_password = self.cleaned_data.get("smtp_password", None)
                    smtp_port = self.cleaned_data.get("smtp_port", None)

                    config = self.instance.config
                    existing_smtp_server = urlparse(config.get("smtp_server", ""))
                    existing_username = ""
                    if existing_smtp_server.username:
                        existing_username = unquote(existing_smtp_server.username)
                    if not smtp_password and existing_username == smtp_username and existing_smtp_server.password:
                        smtp_password = unquote(existing_smtp_server.password)

                    if not smtp_from_email:
                        raise ValidationError(_("You must enter a from email"))

                    parsed = parseaddr(smtp_from_email)
                    if not is_valid_address(parsed[1]):
                        raise ValidationError(_("Please enter a valid email address"))

                    if not smtp_host:
                        raise ValidationError(_("You must enter the SMTP host"))

                    if not smtp_username:
                        raise ValidationError(_("You must enter the SMTP username"))

                    if not smtp_password:
                        raise ValidationError(_("You must enter the SMTP password"))

                    if not smtp_port:
                        raise ValidationError(_("You must enter the SMTP port"))

                    self.cleaned_data["smtp_password"] = smtp_password

                    try:
                        from temba.utils.email import send_custom_smtp_email

                        admin_emails = [admin.email for admin in self.instance.get_admins().order_by("email")]

                        branding = self.instance.get_branding()
                        subject = _("%(name)s SMTP configuration test") % branding
                        body = (
                            _(
                                "This email is a test to confirm the custom SMTP server configuration added to your %(name)s account."
                            )
                            % branding
                        )

                        send_custom_smtp_email(
                            admin_emails,
                            subject,
                            body,
                            smtp_from_email,
                            smtp_host,
                            smtp_port,
                            smtp_username,
                            smtp_password,
                            True,
                        )

                    except smtplib.SMTPException as e:
                        raise ValidationError(
                            _("Failed to send email with STMP server configuration with error '%s'") % str(e)
                        )
                    except Exception:
                        raise ValidationError(_("Failed to send email with STMP server configuration"))

                return self.cleaned_data

            class Meta:
                model = Org
                fields = ("smtp_from_email", "smtp_host", "smtp_username", "smtp_password", "smtp_port", "disconnect")

        form_class = SmtpConfig

        def derive_initial(self):
            initial = super().derive_initial()
            org = self.get_object()
            smtp_server = org.config.get(Org.CONFIG_SMTP_SERVER)
            parsed_smtp_server = urlparse(smtp_server)
            smtp_username = ""
            if parsed_smtp_server.username:
                smtp_username = unquote(parsed_smtp_server.username)
            smtp_password = ""
            if parsed_smtp_server.password:
                smtp_password = unquote(parsed_smtp_server.password)

            initial["smtp_from_email"] = parse_qs(parsed_smtp_server.query).get("from", [None])[0]
            initial["smtp_host"] = parsed_smtp_server.hostname
            initial["smtp_username"] = smtp_username
            initial["smtp_password"] = smtp_password
            initial["smtp_port"] = parsed_smtp_server.port
            initial["disconnect"] = "false"
            return initial

        def form_valid(self, form):
            disconnect = form.cleaned_data.get("disconnect", "false") == "true"
            user = self.request.user
            org = user.get_org()

            if disconnect:
                org.remove_smtp_config(user)
                return HttpResponseRedirect(reverse("orgs.org_home"))
            else:
                smtp_from_email = form.cleaned_data["smtp_from_email"]
                smtp_host = form.cleaned_data["smtp_host"]
                smtp_username = form.cleaned_data["smtp_username"]
                smtp_password = form.cleaned_data["smtp_password"]
                smtp_port = form.cleaned_data["smtp_port"]

                org.add_smtp_config(smtp_from_email, smtp_host, smtp_username, smtp_password, smtp_port, user)

            return super().form_valid(form)

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            org = self.get_object()
            if org.has_smtp_config():
                smtp_server = org.config.get(Org.CONFIG_SMTP_SERVER)
                parsed_smtp_server = urlparse(smtp_server)

                from_email = parse_qs(parsed_smtp_server.query).get("from", [None])[0]
            else:
                from_email = settings.FLOW_FROM_EMAIL

            # populate our context with the from email (just the address)
            context["flow_from_email"] = parseaddr(from_email)[1]

            return context

    class Manage(SmartListView):
        fields = ("plan", "name", "owner", "created_on", "service")
        field_config = {"service": {"label": ""}}
        default_order = ("-credits", "-created_on")
        search_fields = ("name__icontains", "created_by__email__iexact", "config__icontains")
        link_fields = ("name", "owner")
        title = _("Workspaces")

        def get_used(self, obj):
            if not obj.credits:  # pragma: needs cover
                used_pct = 0
            else:
                used_pct = round(100 * float(obj.get_credits_used()) / float(obj.credits))

            used_class = "used-normal"
            if used_pct >= 75:  # pragma: needs cover
                used_class = "used-warning"
            if used_pct >= 90:  # pragma: needs cover
                used_class = "used-alert"
            return mark_safe("<div class='used-pct %s'>%d%%</div>" % (used_class, used_pct))

        def get_plan(self, obj):  # pragma: needs cover
            if not obj.credits:  # pragma: needs cover
                obj.credits = 0

            if obj.plan == "topups":
                return mark_safe(
                    "<div class='num-credits inline-block'><a href='%s'>%s</a></div>%s"
                    % (
                        reverse("orgs.topup_manage") + "?org=%d" % obj.id,
                        format(obj.credits, ",d"),
                        self.get_used(obj),
                    )
                )

            return mark_safe(f"<div class='plan-name'>{obj.plan}</div>")

        def get_owner(self, obj):
            owner = obj.get_owner()

            return mark_safe(
                f"<div class='owner-name'>{escape(owner.first_name)} {escape(owner.last_name)}</div><div class='owner-email'>{owner}</div>"
            )

        def get_service(self, obj):
            url = reverse("orgs.org_service")

            return mark_safe(
                "<div onclick='goto(event)' href='%s?organization=%d' class='service posterize hover-linked text-gray-400'><div class='icon-wand'></div></div>"
                % (url, obj.id)
            )

        def get_name(self, obj):
            flagged = '<span class="flagged">(Flagged)</span>' if obj.is_flagged else ""

            return mark_safe(
                f"<div class='org-name'>{flagged} {escape(obj.name)}</div><div class='org-timezone'>{obj.timezone}</div>"
            )

        def derive_queryset(self, **kwargs):
            queryset = super().derive_queryset(**kwargs)
            queryset = queryset.filter(is_active=True)

            brands = self.request.branding.get("keys")
            if brands:
                queryset = queryset.filter(brand__in=brands)

            anon = self.request.GET.get("anon")
            if anon:
                queryset = queryset.filter(is_anon=str_to_bool(anon))

            suspended = self.request.GET.get("suspended")
            if suspended:
                queryset = queryset.filter(is_suspended=str_to_bool(suspended))

            flagged = self.request.GET.get("flagged")
            if flagged:
                queryset = queryset.filter(is_flagged=str_to_bool(flagged))

            queryset = queryset.annotate(credits=Sum("topups__credits"))
            queryset = queryset.annotate(paid=Sum("topups__price"))

            return queryset

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["searches"] = ["Nyaruka"]
            context["anon_query"] = str_to_bool(self.request.GET.get("anon"))
            context["flagged_query"] = str_to_bool(self.request.GET.get("flagged"))
            context["suspended_query"] = str_to_bool(self.request.GET.get("suspended"))
            return context

        def lookup_field_link(self, context, field, obj):
            if field == "owner":
                owner = obj.get_owner()
                return reverse("users.user_update", args=[owner.pk])
            return super().lookup_field_link(context, field, obj)

        def get_created_by(self, obj):  # pragma: needs cover
            return "%s %s - %s" % (obj.created_by.first_name, obj.created_by.last_name, obj.created_by.email)

    class Update(ComponentFormMixin, SmartUpdateView):
        class Form(forms.ModelForm):
            parent = forms.IntegerField(required=False)
            plan_end = forms.DateTimeField(required=False)

            def clean_parent(self):
                parent = self.cleaned_data.get("parent")
                if parent:
                    return Org.objects.filter(pk=parent).first()

            class Meta:
                model = Org
                fields = (
                    "name",
                    "plan",
                    "plan_end",
                    "brand",
                    "parent",
                    "is_anon",
                    "is_multi_user",
                    "is_multi_org",
                    "is_suspended",
                )

        form_class = Form

        def get_success_url(self):
            return reverse("orgs.org_update", args=[self.get_object().pk])

        def get_gear_links(self):
            links = []

            org = self.get_object()

            if org.is_active:
                links.append(
                    dict(
                        title=_("Topups"),
                        style="button-primary",
                        href="%s?org=%d" % (reverse("orgs.topup_manage"), org.pk),
                    )
                )

                if org.is_flagged:
                    links.append(
                        dict(
                            title=_("Unflag"),
                            style="button-secondary",
                            posterize=True,
                            href="%s?action=unflag" % reverse("orgs.org_update", args=[org.pk]),
                        )
                    )
                else:  # pragma: needs cover
                    links.append(
                        dict(
                            title=_("Flag"),
                            style="button-secondary",
                            posterize=True,
                            href="%s?action=flag" % reverse("orgs.org_update", args=[org.pk]),
                        )
                    )

                if not org.is_verified():
                    links.append(
                        dict(
                            title=_("Verify"),
                            style="button-secondary",
                            posterize=True,
                            href="%s?action=verify" % reverse("orgs.org_update", args=[org.pk]),
                        )
                    )

                if self.request.user.has_perm("orgs.org_delete"):
                    links.append(
                        dict(
                            id="delete-flow",
                            title=_("Delete"),
                            href=reverse("orgs.org_delete", args=[org.pk]),
                            modax="Delete Org",
                        )
                    )
            return links

        def post(self, request, *args, **kwargs):
            if "action" in request.POST:
                action = request.POST["action"]
                if action == "flag":
                    self.get_object().flag()
                elif action == "verify":
                    self.get_object().verify()
                elif action == "unflag":
                    self.get_object().unflag()
                return HttpResponseRedirect(self.get_success_url())
            return super().post(request, *args, **kwargs)

    class Delete(ModalMixin, SmartDeleteView):
        cancel_url = "id@orgs.org_update"
        submit_button_name = _("Delete")
        fields = ("id",)

        def post(self, request, *args, **kwargs):
            self.object = self.get_object()
            self.pre_delete(self.object)
            redirect_url = self.get_redirect_url()
            self.object.release()

            return HttpResponseRedirect(redirect_url)

        def get_redirect_url(self, **kwargs):
            return reverse("orgs.org_update", args=[self.object.pk])

    class Accounts(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class PasswordForm(forms.ModelForm):
            surveyor_password = forms.CharField(
                max_length=128, widget=InputWidget(attrs={"placeholder": "Surveyor Password", "widget_only": True})
            )

            def clean_surveyor_password(self):  # pragma: needs cover
                password = self.cleaned_data.get("surveyor_password", "")
                existing = Org.objects.filter(surveyor_password=password).exclude(pk=self.instance.pk).first()
                if existing:
                    raise forms.ValidationError(_("This password is not valid. Choose a new password and try again."))
                return password

            class Meta:
                model = Org
                fields = ("surveyor_password",)

        form_class = PasswordForm
        success_url = "@orgs.org_home"
        success_message = ""
        submit_button_name = _("Save Changes")
        title = "Logins"
        fields = ("surveyor_password",)

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            org = self.get_object()
            role_summary = []
            for role in OrgRole:
                num_users = org.get_users_with_role(role).count()
                if num_users == 1:
                    role_summary.append(f"1 {role.display}")
                elif num_users > 1:
                    role_summary.append(f"{num_users} {role.display_plural}")

            context["role_summary"] = role_summary
            return context

    class ManageAccounts(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class AccountsForm(forms.ModelForm):
            invite_emails = forms.CharField(
                required=False, widget=InputWidget(attrs={"widget_only": True, "placeholder": _("Email Address")})
            )
            invite_role = forms.ChoiceField(
                choices=[], required=True, initial="V", label=_("Role"), widget=SelectWidget()
            )

            def __init__(self, org, *args, **kwargs):
                super().__init__(*args, **kwargs)

                # orgs see agent role choice if they have an internal ticketing enabled
                has_internal = org.has_internal_ticketing()
                role_choices = [(r.code, r.display) for r in OrgRole if r != OrgRole.AGENT or has_internal]

                self.fields["invite_role"].choices = role_choices

                self.user_rows = []
                self.invite_rows = []
                self.add_per_user_fields(org, role_choices)
                self.add_per_invite_fields(org)

            def add_per_user_fields(self, org: Org, role_choices: list):
                for user in org.get_users():
                    role_field = forms.ChoiceField(
                        choices=role_choices,
                        required=True,
                        initial=org.get_user_role(user).code,
                        label=" ",
                        widget=SelectWidget(),
                    )
                    remove_field = forms.BooleanField(
                        required=False, label=" ", widget=CheckboxWidget(attrs={"widget_only": True})
                    )

                    self.fields.update(
                        OrderedDict([(f"user_{user.id}_role", role_field), (f"user_{user.id}_remove", remove_field)])
                    )
                    self.user_rows.append(
                        {"user": user, "role_field": f"user_{user.id}_role", "remove_field": f"user_{user.id}_remove"}
                    )

            def add_per_invite_fields(self, org: Org):
                for invite in org.invitations.filter(is_active=True).order_by("email"):
                    role_field = forms.ChoiceField(
                        choices=[(r.code, r.display) for r in OrgRole],
                        required=True,
                        initial=invite.role.code,
                        label=" ",
                        widget=SelectWidget(),
                        disabled=True,
                    )
                    remove_field = forms.BooleanField(
                        required=False, label=" ", widget=CheckboxWidget(attrs={"widget_only": True})
                    )

                    self.fields.update(
                        OrderedDict(
                            [(f"invite_{invite.id}_role", role_field), (f"invite_{invite.id}_remove", remove_field)]
                        )
                    )
                    self.invite_rows.append(
                        {
                            "invite": invite,
                            "role_field": f"invite_{invite.id}_role",
                            "remove_field": f"invite_{invite.id}_remove",
                        }
                    )

            def clean_invite_emails(self):
                emails = self.cleaned_data["invite_emails"].lower().strip()
                if emails:
                    email_list = emails.split(",")
                    for email in email_list:
                        try:
                            validate_email(email)
                        except ValidationError:
                            raise forms.ValidationError(_("One of the emails you entered is invalid."))
                return emails

            def get_submitted_roles(self) -> dict:
                """
                Returns a dict of users to roles from the current form data. None role means removal.
                """
                roles = {}

                for row in self.user_rows:
                    role = self.cleaned_data[row["role_field"]]
                    remove = self.cleaned_data[row["remove_field"]]
                    roles[row["user"]] = OrgRole.from_code(role) if not remove else None
                return roles

            def get_submitted_invite_removals(self) -> list:
                """
                Returns a list of invites to be removed.
                """
                invites = []
                for row in self.invite_rows:
                    if self.cleaned_data[row["remove_field"]]:
                        invites.append(row["invite"])
                return invites

            def clean(self):
                super().clean()

                new_roles = self.get_submitted_roles()
                has_admin = False
                for new_role in new_roles.values():
                    if new_role == OrgRole.ADMINISTRATOR:
                        has_admin = True
                        break

                if not has_admin:
                    raise forms.ValidationError(_("A workspace must have at least one administrator."))

            class Meta:
                model = Invitation
                fields = ("invite_emails", "invite_role")

        form_class = AccountsForm
        success_url = "@orgs.org_manage_accounts"
        success_message = ""
        submit_button_name = _("Save Changes")
        title = _("Manage Logins")

        def get_gear_links(self):
            links = []
            if self.request.user.get_org().id != self.get_object().id:
                links.append(dict(title=_("Workspaces"), style="button-light", href=reverse("orgs.org_sub_orgs")))

            links.append(dict(title=_("Home"), style="button-light", href=reverse("orgs.org_home")))
            return links

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["org"] = self.get_object()
            return kwargs

        def post_save(self, obj):
            obj = super().post_save(obj)

            cleaned_data = self.form.cleaned_data
            org = self.get_object()

            # delete any invitations which have been checked for removal
            for invite in self.form.get_submitted_invite_removals():
                org.invitations.filter(id=invite.id).delete()

            # handle any requests for new invitations
            invite_emails = cleaned_data["invite_emails"]
            if invite_emails:
                invite_role = OrgRole.from_code(cleaned_data["invite_role"])
                Invitation.bulk_create_or_update(org, self.request.user, invite_emails.split(","), invite_role)

            # update org users with new roles
            for user, new_role in self.form.get_submitted_roles().items():
                if not new_role:
                    org.remove_user(user)
                elif org.get_user_role(user) != new_role:
                    org.add_user(user, new_role)

                # when a user's role changes, delete any API tokens they're no longer allowed to have
                api_roles = APIToken.get_allowed_roles(org, user)
                for token in APIToken.objects.filter(org=org, user=user).exclude(role__in=api_roles):
                    token.release()

            return obj

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            org = self.get_object()
            context["org"] = org
            context["has_invites"] = org.invitations.filter(is_active=True).exists()
            return context

        def get_success_url(self):
            still_in_org = self.get_object().has_user(self.request.user)

            # if current user no longer belongs to this org, redirect to org chooser
            return reverse("orgs.org_manage_accounts") if still_in_org else reverse("orgs.org_choose")

    class MultiOrgMixin(OrgPermsMixin):
        # if we don't support multi orgs, go home
        def pre_process(self, request, *args, **kwargs):
            response = super().pre_process(request, *args, **kwargs)
            if not response and not request.user.get_org().is_multi_org:
                return HttpResponseRedirect(reverse("orgs.org_home"))
            return response

    class ManageAccountsSubOrg(MultiOrgMixin, ManageAccounts):
        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            org_id = self.request.GET.get("org")
            context["parent"] = Org.objects.filter(id=org_id, parent=self.request.user.get_org()).first()
            return context

        def get_object(self, *args, **kwargs):
            org_id = self.request.GET.get("org")
            return Org.objects.filter(id=org_id, parent=self.request.user.get_org()).first()

        def get_success_url(self):  # pragma: needs cover
            org_id = self.request.GET.get("org")
            return "%s?org=%s" % (reverse("orgs.org_manage_accounts_sub_org"), org_id)

    class Service(SmartFormView):
        class ServiceForm(forms.Form):
            organization = forms.ModelChoiceField(queryset=Org.objects.all(), empty_label=None)
            redirect_url = forms.CharField(required=False)

        form_class = ServiceForm
        fields = ("organization", "redirect_url")

        # valid form means we set our org and redirect to their inbox
        def form_valid(self, form):
            org = form.cleaned_data["organization"]
            self.request.session["org_id"] = org.pk
            success_url = form.cleaned_data["redirect_url"] or reverse("msgs.msg_inbox")
            return HttpResponseRedirect(success_url)

        # invalid form login 'logs out' the user from the org and takes them to the org manage page
        def form_invalid(self, form):
            self.request.session["org_id"] = None
            return HttpResponseRedirect(reverse("orgs.org_manage"))

    class SubOrgs(MultiOrgMixin, InferOrgMixin, SmartListView):
        link_fields = ()
        title = _("Workspaces")

        def derive_fields(self):
            if self.get_object().uses_topups:
                return "credits", "name", "manage", "created_on"
            else:
                return "name", "contacts", "manage", "created_on"

        def get_gear_links(self):
            links = []

            if self.has_org_perm("orgs.org_dashboard"):
                links.append(dict(title=_("Dashboard"), href=reverse("dashboard.dashboard_home")))

            if self.has_org_perm("orgs.org_create_sub_org"):
                links.append(
                    dict(
                        title=_("New Workspace"),
                        href=reverse("orgs.org_create_sub_org"),
                        modax=_("New Workspace"),
                        id="new-workspace",
                    )
                )

            if self.has_org_perm("orgs.org_transfer_credits") and self.get_object().uses_topups:
                links.append(
                    dict(
                        title=_("Transfer Credits"),
                        href=reverse("orgs.org_transfer_credits"),
                        modax=_("Transfer Credits"),
                        id="transfer-credits",
                    )
                )

            return links

        def get_manage(self, obj):  # pragma: needs cover
            if obj == self.get_object():
                return mark_safe(
                    f'<a href="{reverse("orgs.org_manage_accounts")}" class="float-right pr-4"><div class="button-light inline-block ">{_("Manage Logins")}</div></a>'
                )

            if obj.parent:
                return mark_safe(
                    f'<a href="{reverse("orgs.org_manage_accounts_sub_org")}?org={obj.id}" class="float-right pr-4"><div class="button-light inline-block">{_("Manage Logins")}</div></a>'
                )
            return ""

        def get_contacts(self, obj):
            return ContactGroupCount.total_for_org(obj)

        def get_credits(self, obj):
            credits = obj.get_credits_remaining()
            return mark_safe(f'<div class="edit-org"><div class="num-credits">{format(credits, ",d")}</div></div>')

        def get_name(self, obj):
            org_type = "child"
            if not obj.parent:
                org_type = "parent"
            if self.has_org_perm("orgs.org_create_sub_org") and obj.parent:  # pragma: needs cover
                return mark_safe(
                    f"<temba-modax header={_('Update')} endpoint={reverse('orgs.org_edit_sub_org')}?org={obj.id} ><div class='{org_type}-org-name linked'>{escape(obj.name)}</div><div class='org-timezone'>{obj.timezone}</div></temba-modax>"
                )
            return mark_safe(
                f"<div class='org-name'>{escape(obj.name)}</div><div class='org-timezone'>{obj.timezone}</div>"
            )

        def derive_queryset(self, **kwargs):
            queryset = super().derive_queryset(**kwargs)

            # all our children and ourselves
            org = self.get_object()
            ids = [child.id for child in Org.objects.filter(parent=org)]
            ids.append(org.id)

            queryset = queryset.filter(is_active=True)
            queryset = queryset.filter(id__in=ids)
            queryset = queryset.annotate(credits=Sum("topups__credits"))
            queryset = queryset.annotate(paid=Sum("topups__price"))
            return queryset.order_by("-parent", "name")

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["searches"] = ["Nyaruka"]
            return context

        def get_created_by(self, obj):  # pragma: needs cover
            return "%s %s - %s" % (obj.created_by.first_name, obj.created_by.last_name, obj.created_by.email)

    class CreateSubOrg(NonAtomicMixin, MultiOrgMixin, ModalMixin, InferOrgMixin, SmartCreateView):
        class CreateOrgForm(forms.ModelForm):
            name = forms.CharField(
                label=_("Workspace"), help_text=_("The name of your workspace"), widget=InputWidget()
            )

            timezone = TimeZoneFormField(
                help_text=_("The timezone for your workspace"), widget=SelectWidget(attrs={"searchable": True})
            )

            class Meta:
                model = Org
                fields = "__all__"
                widgets = {"date_format": SelectWidget()}

        fields = ("name", "date_format", "timezone")
        form_class = CreateOrgForm
        success_url = "@orgs.org_sub_orgs"
        permission = "orgs.org_create_sub_org"

        def derive_initial(self):
            initial = super().derive_initial()
            parent = self.request.user.get_org()
            initial["timezone"] = parent.timezone
            initial["date_format"] = parent.date_format
            return initial

        def form_valid(self, form):
            self.object = form.save(commit=False)
            parent = self.org
            parent.create_sub_org(self.object.name, self.object.timezone, self.request.user)
            if "HTTP_X_PJAX" not in self.request.META:
                return HttpResponseRedirect(self.get_success_url())
            else:  # pragma: no cover
                response = self.render_to_response(
                    self.get_context_data(
                        form=form,
                        success_url=self.get_success_url(),
                        success_script=getattr(self, "success_script", None),
                    )
                )
                response["Temba-Success"] = self.get_success_url()
                return response

    class Choose(SmartFormView):
        class ChooseForm(forms.Form):
            organization = forms.ModelChoiceField(queryset=Org.objects.all(), empty_label=None)

        form_class = ChooseForm
        success_url = "@msgs.msg_inbox"
        fields = ("organization",)
        title = _("Select your Workspace")

        def get_user_orgs(self):
            return self.request.user.get_user_orgs(self.request.branding.get("keys"))

        def pre_process(self, request, *args, **kwargs):
            user = self.request.user
            if user.is_authenticated:
                user_orgs = self.get_user_orgs()
                if user.is_superuser:
                    return HttpResponseRedirect(reverse("orgs.org_manage"))

                elif user_orgs.count() == 1:
                    org = user_orgs[0]
                    self.request.session["org_id"] = org.pk
                    if org.get_user_role(user) == OrgRole.SURVEYOR:
                        return HttpResponseRedirect(reverse("orgs.org_surveyor"))

                    return HttpResponseRedirect(self.get_success_url())  # pragma: needs cover

                elif user_orgs.count() == 0:  # pragma: needs cover
                    if user.is_support():
                        return HttpResponseRedirect(reverse("orgs.org_manage"))

                    # for regular users, if there's no orgs, log them out with a message
                    messages.info(request, _("No organizations for this account, please contact your administrator."))
                    logout(request)
                    return HttpResponseRedirect(reverse("users.user_login"))
            return None  # pragma: needs cover

        def get_context_data(self, **kwargs):  # pragma: needs cover
            context = super().get_context_data(**kwargs)
            context["orgs"] = self.get_user_orgs()
            return context

        def has_permission(self, request, *args, **kwargs):
            return self.request.user.is_authenticated

        def customize_form_field(self, name, field):  # pragma: needs cover
            if name == "organization":
                field.widget.choices.queryset = self.get_user_orgs()
            return field

        def form_valid(self, form):  # pragma: needs cover
            org = form.cleaned_data["organization"]

            if org in self.get_user_orgs():
                self.request.session["org_id"] = org.pk
            else:
                return HttpResponseRedirect(reverse("orgs.org_choose"))

            if org.get_user_role(self.request.user) == OrgRole.SURVEYOR:
                return HttpResponseRedirect(reverse("orgs.org_surveyor"))

            return HttpResponseRedirect(self.get_success_url())

    class CreateLogin(SmartUpdateView):
        title = ""
        form_class = OrgSignupForm
        fields = ("first_name", "last_name", "email", "password")
        success_message = ""
        success_url = "@msgs.msg_inbox"
        submit_button_name = _("Create")
        permission = False

        def pre_process(self, request, *args, **kwargs):
            org = self.get_object()
            if not org:  # pragma: needs cover
                messages.info(
                    request, _("Your invitation link is invalid. Please contact your workspace administrator.")
                )
                return HttpResponseRedirect(reverse("public.public_index"))
            return None

        def pre_save(self, obj):
            obj = super().pre_save(obj)

            user = Org.create_user(self.form.cleaned_data["email"], self.form.cleaned_data["password"])

            user.first_name = self.form.cleaned_data["first_name"]
            user.last_name = self.form.cleaned_data["last_name"]
            user.save()

            self.invitation = self.get_invitation()

            # log the user in
            user = authenticate(username=user.username, password=self.form.cleaned_data["password"])
            login(self.request, user)

            role = OrgRole.from_code(self.invitation.user_group) or OrgRole.VIEWER
            obj.add_user(user, role)

            # make the invitation inactive
            self.invitation.is_active = False
            self.invitation.save()

            return obj

        def get_success_url(self):
            if self.invitation.user_group == "S":
                return reverse("orgs.org_surveyor")
            return super().get_success_url()

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r"^%s/%s/(?P<secret>\w+)/$" % (path, action)

        def get_invitation(self, **kwargs):
            invitation = None
            secret = self.kwargs.get("secret")
            invitations = Invitation.objects.filter(secret=secret, is_active=True)
            if invitations:
                invitation = invitations[0]
            return invitation

        def get_object(self, **kwargs):
            invitation = self.get_invitation()
            if invitation:
                return invitation.org
            return None  # pragma: needs cover

        def derive_title(self):
            org = self.get_object()
            return _("Join %(name)s") % {"name": org.name}

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            context["secret"] = self.kwargs.get("secret")
            context["org"] = self.get_object()

            return context

    class Join(SmartUpdateView):
        class JoinForm(forms.ModelForm):
            class Meta:
                model = Org
                fields = ()

        success_message = ""
        form_class = JoinForm
        success_url = "@msgs.msg_inbox"
        submit_button_name = _("Join")
        permission = False

        def pre_process(self, request, *args, **kwargs):  # pragma: needs cover
            secret = self.kwargs.get("secret")

            org = self.get_object()
            if not org:
                messages.info(
                    request, _("Your invitation link has expired. Please contact your workspace administrator.")
                )
                return HttpResponseRedirect(reverse("public.public_index"))

            if not request.user.is_authenticated:
                return HttpResponseRedirect(reverse("orgs.org_create_login", args=[secret]))
            return None

        def derive_title(self):  # pragma: needs cover
            org = self.get_object()
            return _("Join %(name)s") % {"name": org.name}

        def save(self, org):  # pragma: needs cover
            org = self.get_object()
            self.invitation = self.get_invitation()
            if org:
                role = OrgRole.from_code(self.invitation.user_group) or OrgRole.VIEWER
                org.add_user(self.request.user, role)

                # make the invitation inactive
                self.invitation.is_active = False
                self.invitation.save()

                # set the active org on this user
                self.request.user.set_org(org)
                self.request.session["org_id"] = org.pk

        def get_success_url(self):  # pragma: needs cover
            if self.invitation.user_group == "S":
                return reverse("orgs.org_surveyor")

            return super().get_success_url()

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r"^%s/%s/(?P<secret>\w+)/$" % (path, action)

        def get_invitation(self, **kwargs):  # pragma: needs cover
            invitation = None
            secret = self.kwargs.get("secret")
            invitations = Invitation.objects.filter(secret=secret, is_active=True)
            if invitations:
                invitation = invitations[0]
            return invitation

        def get_object(self, **kwargs):  # pragma: needs cover
            invitation = self.get_invitation()
            if invitation:
                return invitation.org

        def get_context_data(self, **kwargs):  # pragma: needs cover
            context = super().get_context_data(**kwargs)

            context["org"] = self.get_object()
            return context

    class Surveyor(SmartFormView):
        class PasswordForm(forms.Form):
            surveyor_password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Password"}))

            def clean_surveyor_password(self):
                password = self.cleaned_data["surveyor_password"]
                org = Org.objects.filter(surveyor_password=password).first()
                if not org:
                    raise forms.ValidationError(
                        _("Invalid surveyor password, please check with your project leader and try again.")
                    )
                self.cleaned_data["org"] = org
                return password

        class RegisterForm(PasswordForm):
            surveyor_password = forms.CharField(widget=forms.HiddenInput())
            first_name = forms.CharField(
                help_text=_("Your first name"), widget=forms.TextInput(attrs={"placeholder": "First Name"})
            )
            last_name = forms.CharField(
                help_text=_("Your last name"), widget=forms.TextInput(attrs={"placeholder": "Last Name"})
            )
            email = forms.EmailField(
                help_text=_("Your email address"), widget=forms.TextInput(attrs={"placeholder": "Email"})
            )
            password = forms.CharField(
                widget=forms.PasswordInput(attrs={"placeholder": "Password"}),
                required=True,
                validators=[validate_password],
                help_text=_("Your password, at least eight letters please"),
            )

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

            def clean_email(self):
                email = self.cleaned_data["email"]
                if email:
                    if User.objects.filter(username__iexact=email):
                        raise forms.ValidationError(_("That email address is already used"))

                return email.lower()

        permission = None
        form_class = PasswordForm

        def derive_initial(self):
            initial = super().derive_initial()
            initial["surveyor_password"] = self.request.POST.get("surveyor_password", "")
            return initial

        def get_context_data(self, **kwargs):
            context = super().get_context_data()
            context["form"] = self.form
            context["step"] = self.get_step()

            for key, field in self.form.fields.items():
                context[key] = field

            return context

        def get_success_url(self):
            return reverse("orgs.org_surveyor")

        def get_form_class(self):
            if self.get_step() == 2:
                return OrgCRUDL.Surveyor.RegisterForm
            else:
                return OrgCRUDL.Surveyor.PasswordForm

        def get_step(self):
            return 2 if "first_name" in self.request.POST else 1

        def form_valid(self, form):
            if self.get_step() == 1:

                org = self.form.cleaned_data.get("org", None)

                context = self.get_context_data()
                context["step"] = 2
                context["org"] = org

                self.form = OrgCRUDL.Surveyor.RegisterForm(initial=self.derive_initial())
                context["form"] = self.form

                return self.render_to_response(context)
            else:

                # create our user
                username = self.form.cleaned_data["email"]
                user = Org.create_user(username, self.form.cleaned_data["password"])

                user.first_name = self.form.cleaned_data["first_name"]
                user.last_name = self.form.cleaned_data["last_name"]
                user.save()

                # log the user in
                user = authenticate(username=user.username, password=self.form.cleaned_data["password"])
                login(self.request, user)

                org = self.form.cleaned_data["org"]
                org.surveyors.add(user)

                surveyors_group = Group.objects.get(name="Surveyors")
                token = APIToken.get_or_create(org, user, role=surveyors_group)

                org_name = urlquote(org.name)

                return HttpResponseRedirect(
                    f"{self.get_success_url()}?org={org_name}&uuid={str(org.uuid)}&token={token}&user={username}"
                )

        def form_invalid(self, form):
            return super().form_invalid(form)

        def derive_title(self):
            return _("Welcome!")

        def get_template_names(self):
            if (
                "android" in self.request.META.get("HTTP_X_REQUESTED_WITH", "")
                or "mobile" in self.request.GET
                or "Android" in self.request.META.get("HTTP_USER_AGENT", "")
            ):
                return ["orgs/org_surveyor_mobile.haml"]
            else:
                return super().get_template_names()

    class Grant(NonAtomicMixin, SmartCreateView):
        title = _("Create Workspace Account")
        form_class = OrgGrantForm
        fields = ("first_name", "last_name", "email", "password", "name", "timezone", "credits")
        success_message = "Workspace successfully created."
        submit_button_name = _("Create")
        permission = "orgs.org_grant"
        success_url = "@orgs.org_grant"

        def create_user(self):
            user = User.objects.filter(username__iexact=self.form.cleaned_data["email"]).first()
            if not user:
                user = Org.create_user(self.form.cleaned_data["email"], self.form.cleaned_data["password"])

            user.first_name = self.form.cleaned_data["first_name"]
            user.last_name = self.form.cleaned_data["last_name"]
            user.save()

            # set our language to the default for the site
            language = self.request.branding.get("language", settings.DEFAULT_LANGUAGE)
            user_settings = user.get_settings()
            user_settings.language = language
            user_settings.save()

            return user

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["branding"] = self.request.branding
            return kwargs

        def pre_save(self, obj):

            obj = super().pre_save(obj)

            self.user = self.create_user()

            obj.created_by = self.user
            obj.modified_by = self.user
            obj.brand = self.request.branding.get("brand", settings.DEFAULT_BRAND)
            obj.plan = self.request.branding.get("default_plan", settings.DEFAULT_PLAN)

            if obj.timezone.zone in pytz.country_timezones("US"):
                obj.date_format = Org.DATE_FORMAT_MONTH_FIRST

            return obj

        def get_welcome_size(self):  # pragma: needs cover
            return self.form.cleaned_data["credits"]

        def post_save(self, obj):
            obj = super().post_save(obj)
            obj.add_user(self.user, OrgRole.ADMINISTRATOR)

            if not self.request.user.is_anonymous and self.request.user.has_perm(
                "orgs.org_grant"
            ):  # pragma: needs cover
                obj.add_user(self.request.user, OrgRole.ADMINISTRATOR)

            obj.initialize(branding=obj.get_branding(), topup_size=self.get_welcome_size())

            return obj

    class Signup(ComponentFormMixin, Grant):
        title = _("Sign Up")
        form_class = OrgSignupForm
        permission = None
        success_message = ""
        submit_button_name = _("Save")

        def get_success_url(self):
            return "%s?start" % reverse("public.public_welcome")

        def pre_process(self, request, *args, **kwargs):
            # if our brand doesn't allow signups, then redirect to the homepage
            if not request.branding.get("allow_signups", False):  # pragma: needs cover
                return HttpResponseRedirect(reverse("public.public_index"))

            else:
                return super().pre_process(request, *args, **kwargs)

        def derive_initial(self):
            initial = super().get_initial()
            initial["email"] = self.request.POST.get("email", self.request.GET.get("email", None))
            return initial

        def get_welcome_size(self):
            welcome_topup_size = self.request.branding.get("welcome_topup", 0)
            return welcome_topup_size

        def post_save(self, obj):
            user = authenticate(username=self.user.username, password=self.form.cleaned_data["password"])

            # setup user tracking before creating Org in super().post_save
            analytics.identify(user, brand=self.request.branding["slug"], org=obj)
            analytics.track(email=user.username, event_name="temba.org_signup", properties=dict(org=obj.name))

            obj = super().post_save(obj)

            self.request.session["org_id"] = obj.pk

            login(self.request, user)

            return obj

    class Resthooks(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class ResthookForm(forms.ModelForm):
            resthook = forms.SlugField(
                required=False,
                label=_("New Event"),
                help_text="Enter a name for your event. ex: new-registration",
                widget=InputWidget(),
            )

            def add_resthook_fields(self):
                resthooks = []
                field_mapping = []

                for resthook in self.instance.get_resthooks():
                    check_field = forms.BooleanField(required=False)
                    field_name = "resthook_%d" % resthook.pk

                    field_mapping.append((field_name, check_field))
                    resthooks.append(dict(resthook=resthook, field=field_name))

                self.fields = OrderedDict(list(self.fields.items()) + field_mapping)
                return resthooks

            def clean_resthook(self):
                new_resthook = self.data.get("resthook")

                if new_resthook:
                    if self.instance.resthooks.filter(is_active=True, slug__iexact=new_resthook):
                        raise ValidationError("This event name has already been used")

                return new_resthook

            class Meta:
                model = Org
                fields = ("id", "resthook")

        form_class = ResthookForm
        success_message = ""

        def get_form(self):
            form = super().get_form()
            self.current_resthooks = form.add_resthook_fields()
            return form

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["current_resthooks"] = self.current_resthooks
            return context

        def pre_save(self, obj):
            from temba.api.models import Resthook

            new_resthook = self.form.data.get("resthook")
            if new_resthook:
                Resthook.get_or_create(obj, new_resthook, self.request.user)

            # release any resthooks that the user removed
            for resthook in self.current_resthooks:
                if self.form.data.get(resthook["field"]):
                    resthook["resthook"].release(self.request.user)

            return super().pre_save(obj)

    class Token(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class TokenForm(forms.ModelForm):
            class Meta:
                model = Org
                fields = ("id",)

        form_class = TokenForm
        success_url = "@orgs.org_home"
        success_message = ""

        def get_context_data(self, **kwargs):
            from temba.api.models import WebHookResult

            context = super().get_context_data(**kwargs)
            context["failed_webhooks"] = WebHookResult.get_recent_errored(self.request.user.get_org()).exists()
            return context

    class Prometheus(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class ToggleForm(forms.ModelForm):
            class Meta:
                model = Org
                fields = ("id",)

        form_class = ToggleForm
        success_url = "@orgs.org_home"
        success_message = ""

        def post_save(self, obj):
            group = Group.objects.get(name="Prometheus")
            user = self.request.user
            org = user.get_org()

            # look up to see if there is a prometheus token on this org
            token = APIToken.objects.filter(is_active=True, org=org, role=group)

            # if our org has a token, disable it
            if token:
                token.update(is_active=False)

            # otherwise, create a new token (it is created for user but shared for org)
            else:
                APIToken.get_or_create(org, user, group)

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)

            user = self.request.user
            org = user.get_org()
            token = APIToken.objects.filter(is_active=True, org=org, role=Group.objects.get(name="Prometheus")).first()
            if token:
                context["prometheus_token"] = token.key
                context["prometheus_url"] = f"https://{org.get_branding()['domain']}/mr/org/{org.uuid}/metrics"

            return context

    class Chatbase(ComponentFormMixin, InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class ChatbaseForm(forms.ModelForm):
            agent_name = forms.CharField(
                max_length=255, label=_("Agent Name"), required=False, help_text="Enter your Chatbase Agent's name"
            )
            api_key = forms.CharField(
                max_length=255,
                label=_("API Key"),
                required=False,
                help_text="You can find your Agent's API Key in your chatbase account",
            )
            version = forms.CharField(
                max_length=10, label=_("Version"), required=False, help_text="Any will do, e.g. 1.0, 1.2.1"
            )
            disconnect = forms.CharField(widget=forms.HiddenInput, max_length=6, required=True)

            def clean(self):
                super().clean()
                if self.cleaned_data.get("disconnect", "false") == "false":
                    agent_name = self.cleaned_data.get("agent_name")
                    api_key = self.cleaned_data.get("api_key")

                    if not agent_name or not api_key:
                        raise ValidationError(
                            _("Missing data: Agent Name or API Key." "Please check them again and retry.")
                        )

                return self.cleaned_data

            class Meta:
                model = Org
                fields = ("agent_name", "api_key", "version", "disconnect")

        success_message = ""
        success_url = "@orgs.org_home"
        form_class = ChatbaseForm

        def derive_initial(self):
            initial = super().derive_initial()
            org = self.get_object()
            config = org.config
            initial["agent_name"] = config.get(Org.CONFIG_CHATBASE_AGENT_NAME, "")
            initial["api_key"] = config.get(Org.CONFIG_CHATBASE_API_KEY, "")
            initial["version"] = config.get(Org.CONFIG_CHATBASE_VERSION, "")
            initial["disconnect"] = "false"
            return initial

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            (chatbase_api_key, chatbase_version) = self.object.get_chatbase_credentials()
            if chatbase_api_key:
                config = self.object.config
                agent_name = config.get(Org.CONFIG_CHATBASE_AGENT_NAME)
                context["chatbase_agent_name"] = agent_name

            return context

        def form_valid(self, form):
            user = self.request.user
            org = user.get_org()

            agent_name = form.cleaned_data.get("agent_name")
            api_key = form.cleaned_data.get("api_key")
            version = form.cleaned_data.get("version")
            disconnect = form.cleaned_data.get("disconnect", "false") == "true"

            if disconnect:
                org.remove_chatbase_account(user)
                return HttpResponseRedirect(reverse("orgs.org_home"))
            elif api_key:
                org.connect_chatbase(agent_name, api_key, version, user)

            return super().form_valid(form)

    class Home(FormaxMixin, InferOrgMixin, OrgPermsMixin, SmartReadView):
        title = _("Your Account")

        def get_gear_links(self):
            links = []

            if self.has_org_perm("channels.channel_claim"):
                links.append(dict(title=_("Add Channel"), href=reverse("channels.channel_claim")))

            if self.has_org_perm("classifiers.classifier_connect"):
                links.append(dict(title=_("Add Classifier"), href=reverse("classifiers.classifier_connect")))

            if self.has_org_perm("tickets.ticketer_connect"):
                links.append(dict(title=_("Add Ticketing Service"), href=reverse("tickets.ticketer_connect")))

            if len(links) > 0:
                links.append(dict(divider=True))

            if self.has_org_perm("orgs.org_export"):
                links.append(dict(title=_("Export"), href=reverse("orgs.org_export")))

            if self.has_org_perm("orgs.org_import"):
                links.append(dict(title=_("Import"), href=reverse("orgs.org_import")))

            if settings.HELP_URL:  # pragma: needs cover
                if len(links) > 0:
                    links.append(dict(divider=True))

                links.append(dict(title=_("Help"), href=settings.HELP_URL))

            if len(links) > 0:
                links.append(dict(divider=True))

            links.append(
                dict(
                    title=_("Sign Out"),
                    style="button-light",
                    href=f"{reverse('users.user_logout')}?next={reverse('users.user_login')}",
                )
            )

            return links

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)
            # context['channels'] = Channel.objects.filter(org=self.request.user.get_org(), is_active=True, parent=None).order_by("-role")
            return context

        def add_channel_section(self, formax, channel):

            if self.has_org_perm("channels.channel_read"):
                from temba.channels.views import get_channel_read_url

                formax.add_section(
                    "channel", get_channel_read_url(channel), icon=channel.get_type().icon, action="link"
                )

        def add_classifier_section(self, formax, classifier):

            if self.has_org_perm("classifiers.classifier_read"):
                formax.add_section(
                    "classifier",
                    reverse("classifiers.classifier_read", args=[classifier.uuid]),
                    icon=classifier.get_type().icon,
                    action="link",
                )

        def add_ticketer_section(self, formax, ticketer):

            if self.has_org_perm("tickets.ticket_filter"):
                formax.add_section(
                    "tickets",
                    reverse("tickets.ticket_filter", args=[ticketer.uuid]),
                    icon=ticketer.get_type().icon,
                    action="link",
                )

        def derive_formax_sections(self, formax, context):

            # add the channel option if we have one
            user = self.request.user
            org = user.get_org()

            # if we are on the topups plan, show our usual credits view
            if org.plan == settings.TOPUP_PLAN:
                if self.has_org_perm("orgs.topup_list"):
                    formax.add_section("topups", reverse("orgs.topup_list"), icon="icon-coins", action="link")

            else:
                if self.has_org_perm("orgs.org_plan"):  # pragma: needs cover
                    formax.add_section("plan", reverse("orgs.org_plan"), icon="icon-credit", action="summary")

            if self.has_org_perm("channels.channel_update"):
                # get any channel thats not a delegate
                channels = Channel.objects.filter(org=org, is_active=True, parent=None).order_by("-role")
                for channel in channels:
                    self.add_channel_section(formax, channel)

                twilio_client = org.get_twilio_client()
                if twilio_client:  # pragma: needs cover
                    formax.add_section("twilio", reverse("orgs.org_twilio_account"), icon="icon-channel-twilio")

                nexmo_client = org.get_nexmo_client()
                if nexmo_client:  # pragma: needs cover
                    formax.add_section("nexmo", reverse("orgs.org_nexmo_account"), icon="icon-channel-nexmo")

            if self.has_org_perm("classifiers.classifier_read"):
                classifiers = org.classifiers.filter(is_active=True).order_by("created_on")
                for classifier in classifiers:
                    self.add_classifier_section(formax, classifier)

            if self.has_org_perm("tickets.ticket_filter"):
                from temba.tickets.types.internal import InternalType

                ext_ticketers = (
                    org.ticketers.filter(is_active=True)
                    .exclude(ticketer_type=InternalType.slug)
                    .order_by("created_on")
                )
                for ticketer in ext_ticketers:
                    self.add_ticketer_section(formax, ticketer)

            if self.has_org_perm("orgs.org_profile"):
                formax.add_section("user", reverse("orgs.user_edit"), icon="icon-user", action="redirect")

            if self.has_org_perm("orgs.org_two_factor"):
                if user.get_settings().two_factor_enabled:
                    formax.add_section(
                        "two_factor", reverse("orgs.user_two_factor_tokens"), icon="icon-two-factor", action="link"
                    )
                else:
                    formax.add_section(
                        "two_factor", reverse("orgs.user_two_factor_enable"), icon="icon-two-factor", action="link"
                    )

            if self.has_org_perm("orgs.org_edit"):
                formax.add_section("org", reverse("orgs.org_edit"), icon="icon-office")

            # only pro orgs get multiple users
            if self.has_org_perm("orgs.org_manage_accounts") and org.is_multi_user:
                formax.add_section("accounts", reverse("orgs.org_accounts"), icon="icon-users", action="redirect")

            if self.has_org_perm("orgs.org_languages"):
                formax.add_section("languages", reverse("orgs.org_languages"), icon="icon-language")

            if self.has_org_perm("orgs.org_country"):
                formax.add_section("country", reverse("orgs.org_country"), icon="icon-location2")

            if self.has_org_perm("orgs.org_smtp_server"):
                formax.add_section("email", reverse("orgs.org_smtp_server"), icon="icon-envelop")

            if self.has_org_perm("orgs.org_dtone_account"):
                if not self.object.is_connected_to_dtone():
                    formax.add_section(
                        "dtone",
                        reverse("orgs.org_dtone_account"),
                        icon="icon-dtone",
                        action="redirect",
                        button=_("Connect"),
                    )
                else:  # pragma: needs cover
                    formax.add_section(
                        "dtone", reverse("orgs.org_dtone_account"), icon="icon-dtone", action="redirect", nobutton=True
                    )

            if self.has_org_perm("orgs.org_chatbase"):
                (chatbase_api_key, chatbase_version) = self.object.get_chatbase_credentials()
                if not chatbase_api_key:
                    formax.add_section(
                        "chatbase",
                        reverse("orgs.org_chatbase"),
                        icon="icon-chatbase",
                        action="redirect",
                        button=_("Connect"),
                    )
                else:  # pragma: needs cover
                    formax.add_section(
                        "chatbase",
                        reverse("orgs.org_chatbase"),
                        icon="icon-chatbase",
                        action="redirect",
                        nobutton=True,
                    )

            if self.has_org_perm("orgs.org_token"):
                formax.add_section("token", reverse("orgs.org_token"), icon="icon-cloud-upload", nobutton=True)

            if self.has_org_perm("orgs.org_prometheus"):
                formax.add_section("prometheus", reverse("orgs.org_prometheus"), icon="icon-prometheus", nobutton=True)

            if self.has_org_perm("orgs.org_resthooks"):
                formax.add_section(
                    "resthooks", reverse("orgs.org_resthooks"), icon="icon-cloud-lightning", dependents="resthooks"
                )

            # show globals and archives
            formax.add_section("globals", reverse("globals.global_list"), icon="icon-global", action="link")
            formax.add_section("archives", reverse("archives.archive_message"), icon="icon-box", action="link")

    class DtoneAccount(InferOrgMixin, OrgPermsMixin, SmartUpdateView):

        success_message = ""

        class DTOneAccountForm(forms.ModelForm):
            account_login = forms.CharField(label=_("Login"), required=False, widget=InputWidget())
            airtime_api_token = forms.CharField(label=_("API Token"), required=False, widget=InputWidget())
            disconnect = forms.CharField(widget=forms.HiddenInput, max_length=6, required=False)

            def clean(self):
                super().clean()
                if self.cleaned_data.get("disconnect", "false") == "false":
                    account_login = self.cleaned_data.get("account_login", None)
                    airtime_api_token = self.cleaned_data.get("airtime_api_token", None)

                    try:
                        from temba.airtime.dtone import DTOneClient

                        client = DTOneClient(account_login, airtime_api_token)
                        response = client.ping()

                        error_code = int(response.get("error_code", None))
                        info_txt = response.get("info_txt", None)
                        error_txt = response.get("error_txt", None)

                    except Exception:
                        raise ValidationError(
                            _("Your DT One API key and secret seem invalid. Please check them again and retry.")
                        )

                    if error_code != 0 and info_txt != "pong":
                        raise ValidationError(
                            _("Connecting to your DT One account failed with error text: %s") % error_txt
                        )

                return self.cleaned_data

            class Meta:
                model = Org
                fields = ("account_login", "airtime_api_token", "disconnect")

        form_class = DTOneAccountForm
        submit_button_name = "Save"
        success_url = "@orgs.org_home"

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            if self.object.is_connected_to_dtone():
                config = self.object.config
                account_login = config.get(Org.CONFIG_DTONE_LOGIN)
                context["dtone_account_login"] = account_login

            return context

        def derive_initial(self):
            initial = super().derive_initial()
            config = self.object.config
            initial["account_login"] = config.get(Org.CONFIG_DTONE_LOGIN)
            initial["airtime_api_token"] = config.get(Org.CONFIG_DTONE_API_TOKEN)
            initial["disconnect"] = "false"
            return initial

        def form_valid(self, form):
            user = self.request.user
            org = user.get_org()
            disconnect = form.cleaned_data.get("disconnect", "false") == "true"
            if disconnect:
                org.remove_dtone_account(user)
                return HttpResponseRedirect(reverse("orgs.org_home"))
            else:
                account_login = form.cleaned_data["account_login"]
                airtime_api_token = form.cleaned_data["airtime_api_token"]

                org.connect_dtone(account_login, airtime_api_token, user)
                org.refresh_dtone_account_currency()
                return super().form_valid(form)

    class TwilioAccount(ComponentFormMixin, InferOrgMixin, OrgPermsMixin, SmartUpdateView):

        success_message = ""

        class TwilioKeys(forms.ModelForm):
            account_sid = forms.CharField(max_length=128, label=_("Account SID"), required=False)
            account_token = forms.CharField(max_length=128, label=_("Account Token"), required=False)
            disconnect = forms.CharField(widget=forms.HiddenInput, max_length=6, required=True)

            def clean(self):
                super().clean()
                if self.cleaned_data.get("disconnect", "false") == "false":
                    account_sid = self.cleaned_data.get("account_sid", None)
                    account_token = self.cleaned_data.get("account_token", None)

                    if not account_sid:
                        raise ValidationError(_("You must enter your Twilio Account SID"))

                    if not account_token:  # pragma: needs cover
                        raise ValidationError(_("You must enter your Twilio Account Token"))

                    try:
                        client = Client(account_sid, account_token)

                        # get the actual primary auth tokens from twilio and use them
                        account = client.api.account.fetch()
                        self.cleaned_data["account_sid"] = account.sid
                        self.cleaned_data["account_token"] = account.auth_token
                    except Exception:  # pragma: needs cover
                        raise ValidationError(
                            _("The Twilio account SID and Token seem invalid. Please check them again and retry.")
                        )

                return self.cleaned_data

            class Meta:
                model = Org
                fields = ("account_sid", "account_token", "disconnect")

        form_class = TwilioKeys

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            client = self.object.get_twilio_client()
            if client:
                account_sid = client.auth[0]
                sid_length = len(account_sid)
                context["account_sid"] = "%s%s" % ("\u066D" * (sid_length - 16), account_sid[-16:])
            return context

        def derive_initial(self):
            initial = super().derive_initial()
            config = self.object.config
            initial["account_sid"] = config[Org.CONFIG_TWILIO_SID]
            initial["account_token"] = config[Org.CONFIG_TWILIO_TOKEN]
            initial["disconnect"] = "false"
            return initial

        def form_valid(self, form):
            disconnect = form.cleaned_data.get("disconnect", "false") == "true"
            user = self.request.user
            org = user.get_org()

            if disconnect:
                org.remove_twilio_account(user)
                return HttpResponseRedirect(reverse("orgs.org_home"))
            else:
                account_sid = form.cleaned_data["account_sid"]
                account_token = form.cleaned_data["account_token"]

                org.connect_twilio(account_sid, account_token, user)
                return super().form_valid(form)

    class Edit(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class OrgForm(forms.ModelForm):
            name = forms.CharField(max_length=128, label=_("Workspace Name"), help_text="", widget=InputWidget())
            timezone = TimeZoneFormField(
                label=_("Timezone"), help_text="", widget=SelectWidget(attrs={"searchable": True})
            )

            class Meta:
                model = Org
                fields = ("name", "timezone", "date_format")
                widgets = {"date_format": SelectWidget()}

        success_message = ""
        form_class = OrgForm
        fields = ("name", "timezone", "date_format")

        def has_permission(self, request, *args, **kwargs):
            self.org = self.derive_org()
            return self.has_org_perm("orgs.org_edit")

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            sub_orgs = Org.objects.filter(is_active=True, parent=self.get_object())
            context["sub_orgs"] = sub_orgs
            return context

    class EditSubOrg(ModalMixin, Edit):

        success_url = "@orgs.org_sub_orgs"

        def get_object(self, *args, **kwargs):
            org_id = self.request.GET.get("org")
            return Org.objects.filter(id=org_id, parent=self.request.user.get_org()).first()

    class TransferCredits(MultiOrgMixin, ModalMixin, InferOrgMixin, SmartFormView):
        class TransferForm(forms.Form):
            class OrgChoiceField(forms.ModelChoiceField):
                def label_from_instance(self, org):
                    return "%s (%s)" % (org.name, "{:,}".format(org.get_credits_remaining()))

            from_org = OrgChoiceField(
                None,
                required=True,
                label=_("From Workspace"),
                help_text=_("Select which workspace to take credits from"),
                widget=SelectWidget(attrs={"searchable": True}),
            )

            to_org = OrgChoiceField(
                None,
                required=True,
                label=_("To Workspace"),
                help_text=_("Select which workspace to receive the credits"),
                widget=SelectWidget(attrs={"searchable": True}),
            )

            amount = forms.IntegerField(
                required=True, label=_("Credits"), help_text=_("How many credits to transfer"), widget=InputWidget()
            )

            def __init__(self, *args, **kwargs):
                org = kwargs["org"]
                del kwargs["org"]

                super().__init__(*args, **kwargs)

                self.fields["from_org"].queryset = Org.objects.filter(Q(parent=org) | Q(id=org.id)).order_by(
                    "-parent", "name", "id"
                )
                self.fields["to_org"].queryset = Org.objects.filter(Q(parent=org) | Q(id=org.id)).order_by(
                    "-parent", "name", "id"
                )

            def clean(self):
                cleaned_data = super().clean()

                if "amount" in cleaned_data and "from_org" in cleaned_data:
                    from_org = cleaned_data["from_org"]

                    if cleaned_data["amount"] > from_org.get_credits_remaining():
                        raise ValidationError(
                            _(
                                "Sorry, %(org_name)s doesn't have enough credits for this transfer. Pick a different workspace to transfer from or reduce the transfer amount."
                            )
                            % dict(org_name=from_org.name)
                        )

        success_url = "@orgs.org_sub_orgs"
        form_class = TransferForm
        fields = ("from_org", "to_org", "amount")
        permission = "orgs.org_transfer_credits"

        def has_permission(self, request, *args, **kwargs):
            self.org = self.request.user.get_org()
            return self.request.user.has_perm(self.permission) or self.has_org_perm(self.permission)

        def get_form_kwargs(self):
            form_kwargs = super().get_form_kwargs()
            form_kwargs["org"] = self.get_object()
            return form_kwargs

        def form_valid(self, form):
            from_org = form.cleaned_data["from_org"]
            to_org = form.cleaned_data["to_org"]
            amount = form.cleaned_data["amount"]

            from_org.allocate_credits(from_org.created_by, to_org, amount)

            response = self.render_to_response(
                self.get_context_data(
                    form=form, success_url=self.get_success_url(), success_script=getattr(self, "success_script", None)
                )
            )

            response["Temba-Success"] = self.get_success_url()
            return response

    class Country(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class CountryForm(forms.ModelForm):
            country = forms.ModelChoiceField(
                Org.get_possible_countries(),
                required=False,
                label=_("The country used for location values. (optional)"),
                help_text="State and district names will be searched against this country.",
                widget=SelectWidget(),
            )

            class Meta:
                model = Org
                fields = ("country",)

        success_message = ""
        form_class = CountryForm

        def has_permission(self, request, *args, **kwargs):
            self.org = self.derive_org()
            return self.request.user.has_perm("orgs.org_country") or self.has_org_perm("orgs.org_country")

    class Languages(InferOrgMixin, OrgPermsMixin, SmartUpdateView):
        class LanguagesForm(forms.ModelForm):

            primary_lang = ArbitraryJsonChoiceField(
                required=False,
                label=_("Primary Language"),
                widget=SelectWidget(
                    attrs={
                        "placeholder": _("Select the primary language for contacts with no language preference"),
                        "searchable": True,
                        "queryParam": "q",
                        "endpoint": reverse_lazy("orgs.org_languages"),
                    }
                ),
            )

            languages = ArbitraryJsonChoiceField(
                required=False,
                label=_("Additional Languages"),
                widget=SelectMultipleWidget(
                    attrs={
                        "placeholder": _("Additional languages you would like to provid translations for"),
                        "searchable": True,
                        "queryParam": "q",
                        "endpoint": reverse_lazy("orgs.org_languages"),
                    }
                ),
            )

            def __init__(self, *args, **kwargs):
                self.org = kwargs["org"]
                del kwargs["org"]
                super().__init__(*args, **kwargs)

            class Meta:
                model = Org
                fields = ("primary_lang", "languages")

        success_message = ""
        form_class = LanguagesForm

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["org"] = self.request.user.get_org()
            return kwargs

        def derive_initial(self):

            initial = super().derive_initial()
            langs = [
                {"name": lang.name, "value": lang.iso_code}
                for lang in self.get_object().languages.filter(orgs=None).order_by("name")
            ]

            initial["languages"] = langs

            if self.object.primary_language:
                lang = self.object.primary_language
                initial["primary_lang"] = [{"name": lang.name, "value": lang.iso_code}]

            return initial

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            languages = [
                lang.name for lang in self.request.user.get_org().languages.filter(orgs=None).order_by("name")
            ]
            lang_count = len(languages)

            if lang_count == 2:
                context["languages"] = _(" and ").join(languages)
            elif lang_count > 2:
                context["languages"] = _("%(lang1)s and %(lang2)s") % dict(
                    lang1=", ".join(languages[:-1]), lang2=languages[-1]
                )
            elif lang_count == 1:
                context["languages"] = languages[0]
            return context

        def get(self, request, *args, **kwargs):
            if request.is_ajax():
                initial = self.request.GET.get("initial", "").split(",")
                matches = []

                if len(initial) > 0:
                    for iso_code in initial:
                        if iso_code:
                            lang = languages.get_language_name(iso_code)
                            matches.append(dict(id=iso_code, text=lang))

                if len(matches) == 0:
                    search = self.request.GET.get("search", self.request.GET.get("q", "")).strip().lower()
                    matches += languages.search_language_names(search)
                return JsonResponse(dict(results=matches))

            return super().get(request, *args, **kwargs)

        def form_valid(self, form):
            user = self.request.user
            primary = form.cleaned_data["primary_lang"]
            if primary:
                primary = primary["value"]

            # remove empty codes and ensure primary is included in list
            iso_codes = [d["value"] for d in form.cleaned_data["languages"] if d["value"]]
            if primary and primary not in iso_codes:
                iso_codes.append(primary)

            self.object.set_languages(user, iso_codes, primary)

            return super().form_valid(form)

        def has_permission(self, request, *args, **kwargs):
            self.org = self.derive_org()
            return self.request.user.has_perm("orgs.org_country") or self.has_org_perm("orgs.org_country")

    class ClearCache(SmartUpdateView):  # pragma: no cover
        fields = ("id",)
        success_message = None
        success_url = "id@orgs.org_update"

        def pre_process(self, request, *args, **kwargs):
            cache = OrgCache(int(request.POST["cache"]))
            num_deleted = self.get_object().clear_caches([cache])
            self.success_message = _("Cleared %(name)s cache for this workspace (%(count)d keys)") % dict(
                name=cache.name, count=num_deleted
            )


class TopUpCRUDL(SmartCRUDL):
    actions = ("list", "create", "read", "manage", "update")
    model = TopUp

    class Read(OrgPermsMixin, SmartReadView):
        def derive_queryset(self, **kwargs):  # pragma: needs cover
            return TopUp.objects.filter(is_active=True, org=self.request.user.get_org()).order_by("-expires_on")

    class List(OrgPermsMixin, SmartListView):
        def derive_queryset(self, **kwargs):
            queryset = TopUp.objects.filter(is_active=True, org=self.request.user.get_org()).order_by("-expires_on")
            return queryset.annotate(
                credits_remaining=ExpressionWrapper(F("credits") - Sum(F("topupcredits__used")), IntegerField())
            )

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["org"] = self.request.user.get_org()

            now = timezone.now()
            context["now"] = now
            context["expiration_period"] = now + timedelta(days=30)

            # show our topups in a meaningful order
            topups = list(self.get_queryset())

            def compare(topup1, topup2):  # pragma: no cover

                # non expired first
                now = timezone.now()
                if topup1.expires_on > now and topup2.expires_on <= now:
                    return -1
                elif topup2.expires_on > now and topup1.expires_on <= now:
                    return 1

                # then push those without credits remaining to the bottom
                if topup1.credits_remaining is None:
                    topup1.credits_remaining = topup1.credits

                if topup2.credits_remaining is None:
                    topup2.credits_remaining = topup2.credits

                if topup1.credits_remaining and not topup2.credits_remaining:
                    return -1
                elif topup2.credits_remaining and not topup1.credits_remaining:
                    return 1

                # sor the rest by their expiration date
                if topup1.expires_on > topup2.expires_on:
                    return -1
                elif topup1.expires_on < topup2.expires_on:
                    return 1

                # if we end up with the same expiration, show the oldest first
                return topup2.id - topup1.id

            topups.sort(key=cmp_to_key(compare))
            context["topups"] = topups
            return context

        def get_template_names(self):
            if "HTTP_X_FORMAX" in self.request.META:
                return ["orgs/topup_list_summary.haml"]
            else:
                return super().get_template_names()

    class Create(ComponentFormMixin, SmartCreateView):
        """
        This is only for root to be able to credit accounts.
        """

        fields = ("credits", "price", "comment")

        def get_success_url(self):
            return reverse("orgs.topup_manage") + ("?org=%d" % self.object.org.id)

        def save(self, obj):
            obj.org = Org.objects.get(pk=self.request.GET["org"])
            return TopUp.create(self.request.user, price=obj.price, credits=obj.credits, org=obj.org)

        def post_save(self, obj):
            obj = super().post_save(obj)
            apply_topups_task.delay(obj.org.id)
            return obj

    class Update(ComponentFormMixin, SmartUpdateView):
        fields = ("is_active", "price", "credits", "expires_on")

        def get_success_url(self):
            return reverse("orgs.topup_manage") + ("?org=%d" % self.object.org.id)

        def post_save(self, obj):
            obj = super().post_save(obj)
            apply_topups_task.delay(obj.org.id)
            return obj

    class Manage(SmartListView):
        """
        This is only for root to be able to manage topups on an account
        """

        fields = ("credits", "price", "comment", "created_on", "expires_on")
        success_url = "@orgs.org_manage"
        default_order = "-expires_on"

        def lookup_field_link(self, context, field, obj):
            return reverse("orgs.topup_update", args=[obj.id])

        def get_price(self, obj):
            if obj.price:
                return "$%.2f" % (obj.price / 100.0)
            else:
                return "-"

        def get_credits(self, obj):
            return format(obj.credits, ",d")

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["org"] = self.org
            return context

        def derive_queryset(self):
            self.org = Org.objects.get(pk=self.request.GET["org"])
            return self.org.topups.all()


class StripeHandler(View):  # pragma: no cover
    """
    Handles WebHook events from Stripe.  We are interested as to when invoices are
    charged by Stripe so we can send the user an invoice email.
    """

    @csrf_exempt
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get(self, request, *args, **kwargs):
        return HttpResponse("ILLEGAL METHOD")

    def post(self, request, *args, **kwargs):
        import stripe
        from temba.orgs.models import Org, TopUp

        # stripe delivers a JSON payload
        stripe_data = json.loads(request.body)

        # but we can't trust just any response, so lets go look up this event
        stripe.api_key = get_stripe_credentials()[1]
        event = stripe.Event.retrieve(stripe_data["id"])

        if not event:
            return HttpResponse("Ignored, no event")

        if not event.livemode:
            return HttpResponse("Ignored, test event")

        # we only care about invoices being paid or failing
        if event.type == "charge.succeeded" or event.type == "charge.failed":
            charge = event.data.object
            charge_date = datetime.fromtimestamp(charge.created)
            description = charge.description
            amount = "$%s" % (Decimal(charge.amount) / Decimal(100)).quantize(Decimal(".01"))

            # look up our customer
            customer = stripe.Customer.retrieve(charge.customer)

            # and our org
            org = Org.objects.filter(stripe_customer=customer.id).first()
            if not org:
                return HttpResponse("Ignored, no org for customer")

            # look up the topup that matches this charge
            topup = TopUp.objects.filter(stripe_charge=charge.id).first()
            if topup and event.type == "charge.failed":
                topup.rollback()
                topup.save()

            # we know this org, trigger an event for a payment succeeding
            if org.administrators.all():
                if event.type == "charge_succeeded":
                    track = "temba.charge_succeeded"
                else:
                    track = "temba.charge_failed"

                context = dict(
                    description=description,
                    invoice_id=charge.id,
                    invoice_date=charge_date.strftime("%b %e, %Y"),
                    amount=amount,
                    org=org.name,
                )

                if getattr(charge, "card", None):
                    context["cc_last4"] = charge.card.last4
                    context["cc_type"] = charge.card.type
                    context["cc_name"] = charge.card.name

                else:
                    context["cc_type"] = "bitcoin"
                    context["cc_name"] = charge.source.bitcoin.address

                admin_email = org.administrators.all().first().email

                analytics.track(admin_email, track, context)
                return HttpResponse("Event '%s': %s" % (track, context))

        # empty response, 200 lets Stripe know we handled it
        return HttpResponse("Ignored, uninteresting event")
