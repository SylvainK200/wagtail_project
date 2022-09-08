import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import get_connection
from django.core.mail.message import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.translation import override

from wagtail.admin.auth import users_with_page_permission
from wagtail.coreutils import camelcase_to_underscore
from wagtail.models import GroupApprovalTask, TaskState, WorkflowState
from wagtail.users.models import UserProfile

logger = logging.getLogger("wagtail.admin")


class OpenedConnection:
    """Context manager for mail connections to ensure they are closed when manually opened"""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.open()
        return self.connection

    def __exit__(self, type, value, traceback):
        self.connection.close()
        return self.connection


def send_mail(subject, message, recipient_list, from_email=None, **kwargs):
    """
    Wrapper around Django's EmailMultiAlternatives as done in send_mail().
    Custom from_email handling and special Auto-Submitted header.
    """
    if from_email is None:
        from_email = settings.DEFAULT_FROM_EMAIL
    email = EmailMultiAlternatives(
        subject, message, from_email, recipient_list, **kwargs
    )
    email.extra_headers = {"Auto-Submitted": "auto-generated"}
    email.send()

def send_moderation_notification(revision, notification, excluded_user=None):
    # Get list of recipients
    if notification == "submitted_for_moderation":
        recipient_users = users_with_page_permission(
            "change", revision.page, for_user=excluded_user
        )
    elif notification == "approved_moderation":
        recipient_users = users_with_page_permission(
            "publish", revision.page, for_user=excluded_user
        )
    elif notification == "rejected_moderation":
        recipient_users = users_with_page_permission(
            "change", revision.page, for_user=excluded_user
        )
    else:
        raise ValueError("Unknown notification type")

    # Get extra context
    context = {
        "revision": revision,
        "page": revision.page,
        "content_type": revision.page.content_type,
        "content_type_name": revision.page.content_type.name,
        "content_type_name_lower": camelcase_to_underscore(
            revision.page.content_type.name
        ),
    }

    return send_notification(recipient_users, notification, context)


def send_notification(recipient_users, notification, extra_context):
    # Get list of email addresses
    recipient_emails = []
    for user in recipient_users:
        if user.email:
            recipient_emails.append(user.email)

    if not recipient_emails:
        return

    # Get notification template
    template_name = "wagtailadmin/notifications/{}.txt".format(notification)

    # Get email subject
    subject = render_to_string(
        "wagtailadmin/notifications/{}.subject.txt".format(notification)
    ).strip()

    # Get email body
    context = {"SITE_ROOT_URL": settings.SITE_ROOT_URL}
    context.update(extra_context)
    message = render_to_string(template_name, context)

    # Send email
    send_mail(subject, message, recipient_emails)


class Notifier:
    """Generic class for sending event notifications: callable, intended to be connected to a signal to send
    notifications using rendered templates."""

    notification = ""
    template_directory = "wagtailadmin/notifications/"

    def __init__(self, valid_classes):
        # the classes of the calling instance that the notifier can handle
        self.valid_classes = valid_classes

    def can_handle(self, instance, **kwargs):
        """Returns True if the Notifier can handle sending the notification from the instance, otherwise False"""
        return isinstance(instance, self.valid_classes)

    def get_valid_recipients(self, instance, **kwargs):
        """Returns a set of the final list of recipients for the notification message"""
        return set()

    def get_template_base_prefix(self, instance, **kwargs):
        return camelcase_to_underscore(type(instance).__name__) + "_"

    def get_context(self, instance, **kwargs):
        return {"settings": settings}

    def get_template_set(self, instance, **kwargs):
        """Return a dictionary of template paths for the templates: by default, a text message"""
        template_base = self.get_template_base_prefix(instance) + self.notification

        template_text = self.template_directory + template_base + ".txt"

        return {
            "text": template_text,
        }

    def send_notifications(self, template_set, context, recipients, **kwargs):
        raise NotImplementedError

    def __call__(self, instance=None, **kwargs):
        """Send notifications from an instance (intended to be the signal sender), returning True if all sent correctly
        and False otherwise"""

        if not self.can_handle(instance, **kwargs):
            return False

        recipients = self.get_valid_recipients(instance, **kwargs)

        if not recipients:
            return True

        template_set = self.get_template_set(instance, **kwargs)

        context = self.get_context(instance, **kwargs)

        return self.send_notifications(template_set, context, recipients, **kwargs)


class EmailNotificationMixin:
    """Mixin for sending email notifications upon events"""

    def get_recipient_users(self, instance, **kwargs):
        """Gets the ideal set of recipient users, without accounting for notification preferences or missing email addresses"""

        return set()

    def get_valid_recipients(self, instance, **kwargs):
        """Filters notification recipients to those allowing the notification type on their UserProfile, and those
        with an email address"""
        return {
            recipient
            for recipient in self.get_recipient_users(instance, **kwargs)
            if recipient.is_active
            and recipient.email
            and getattr(
                UserProfile.get_for_user(recipient),
                self.notification + "_notifications",
            )
        }

    def get_template_set(self, instance, **kwargs):
        """Return a dictionary of template paths for the templates for the email subject and the text and html
        alternatives"""
        template_base = self.get_template_base_prefix(instance) + self.notification

        template_subject = self.template_directory + template_base + "_subject.txt"
        template_text = self.template_directory + template_base + ".txt"
        template_html = self.template_directory + template_base + ".html"

        return {
            "subject": template_subject,
            "text": template_text,
            "html": template_html,
        }

    def send_emails(self, template_set, context, recipients, **kwargs):
        subject = render_to_string(template_set["subject"], context).strip()

        text = render_to_string(template_set["text"], context)

        html = None
        if "html" in template_set:
            html = render_to_string(template_set["html"], context)

        send_mail(
            subject=subject,
            message=text,
            html_message=html,
            recipient_list=recipients,
        )
        
    def send_notifications(self, template_set, context, recipients, **kwargs):
        return self.send_emails(template_set, context, recipients, **kwargs)


class BaseWorkflowStateEmailNotifier(EmailNotificationMixin, Notifier):
    """A base notifier to send email updates for WorkflowState events"""

    def __init__(self):
        super().__init__((WorkflowState,))

    def get_context(self, workflow_state, **kwargs):
        context = super().get_context(workflow_state, **kwargs)
        context["page"] = workflow_state.page
        context["workflow"] = workflow_state.workflow
        return context


class WorkflowStateApprovalEmailNotifier(BaseWorkflowStateEmailNotifier):
    """A notifier to send email updates for WorkflowState approval events"""

    notification = "approved"

    def get_recipient_users(self, workflow_state, **kwargs):
        triggering_user = kwargs.get("user", None)
        recipients = {}
        requested_by = workflow_state.requested_by
        if requested_by != triggering_user:
            recipients = {requested_by}

        return recipients


class WorkflowStateRejectionEmailNotifier(BaseWorkflowStateEmailNotifier):
    """A notifier to send email updates for WorkflowState rejection events"""

    notification = "rejected"

    def get_recipient_users(self, workflow_state, **kwargs):
        triggering_user = kwargs.get("user", None)
        recipients = {}
        requested_by = workflow_state.requested_by
        if requested_by != triggering_user:
            recipients = {requested_by}

        return recipients

    def get_context(self, workflow_state, **kwargs):
        context = super().get_context(workflow_state, **kwargs)
        task_state = workflow_state.current_task_state.specific
        context["task"] = task_state.task
        context["task_state"] = task_state
        context["comment"] = task_state.get_comment()
        return context


class WorkflowStateSubmissionEmailNotifier(BaseWorkflowStateEmailNotifier):
    """A notifier to send email updates for WorkflowState submission events"""

    notification = "submitted"

    def get_recipient_users(self, workflow_state, **kwargs):
        triggering_user = kwargs.get("user", None)
        recipients = get_user_model().objects.none()
        include_superusers = getattr(
            settings, "WAGTAILADMIN_NOTIFICATION_INCLUDE_SUPERUSERS", True
        )
        if include_superusers:
            recipients = get_user_model().objects.filter(is_superuser=True)
        if triggering_user:
            recipients.exclude(pk=triggering_user.pk)

        return recipients

    def get_context(self, workflow_state, **kwargs):
        context = super().get_context(workflow_state, **kwargs)
        context["requested_by"] = workflow_state.requested_by
        return context


class BaseGroupApprovalTaskStateEmailNotifier(EmailNotificationMixin, Notifier):
    """A base notifier to send email updates for GroupApprovalTask events"""

    def __init__(self):
        super().__init__((TaskState,))

    def can_handle(self, instance, **kwargs):
        if super().can_handle(instance, **kwargs) and isinstance(
            instance.task.specific, GroupApprovalTask
        ):
            return True
        return False

    def get_context(self, task_state, **kwargs):
        context = super().get_context(task_state, **kwargs)
        context["page"] = task_state.workflow_state.page
        context["task"] = task_state.task.specific
        return context

    def get_recipient_users(self, task_state, **kwargs):
        triggering_user = kwargs.get("user", None)

        group_members = get_user_model().objects.filter(
            groups__in=task_state.task.specific.groups.all()
        )

        recipients = group_members

        include_superusers = getattr(
            settings, "WAGTAILADMIN_NOTIFICATION_INCLUDE_SUPERUSERS", True
        )
        if include_superusers:
            superusers = get_user_model().objects.filter(is_superuser=True)
            recipients = recipients | superusers

        if triggering_user:
            recipients = recipients.exclude(pk=triggering_user.pk)

        return recipients


class GroupApprovalTaskStateSubmissionEmailNotifier(
    BaseGroupApprovalTaskStateEmailNotifier
):
    """A notifier to send email updates for GroupApprovalTask submission events"""

    notification = "submitted"
