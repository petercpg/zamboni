import json
import urllib
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.utils import translation
from django.utils.datastructures import SortedDict

import commonware.log
import waffle
from tower import ugettext_lazy as _lazy

import amo
from amo.utils import JSONEncoder
from mkt.access import acl
from mkt.comm.utils import create_comm_note
from mkt.constants import comm
from mkt.constants.features import FeatureProfile
from mkt.files.models import File
from mkt.ratings.models import Review
from mkt.reviewers.models import EscalationQueue, RereviewQueue, ReviewerScore
from mkt.site.helpers import absolutify, product_as_dict
from mkt.site.mail import send_mail_jinja
from mkt.site.models import manual_order
from mkt.translations.query import order_by_translation
from mkt.translations.utils import to_language
from mkt.versions.models import Version
from mkt.webapps.models import Webapp
from mkt.webapps.tasks import set_storefront_data


log = commonware.log.getLogger('z.mailer')


def send_reviewer_mail(subject, template, context, emails, perm_setting=None,
                       cc=None, attachments=None, reply_to=None):
    if not reply_to:
        reply_to = settings.MKT_REVIEWERS_EMAIL

    # Link to our newfangled "Account Settings" page.
    manage_url = absolutify('/settings') + '#notifications'
    send_mail_jinja(subject, template, context, recipient_list=emails,
                    from_email=settings.MKT_REVIEWERS_EMAIL,
                    use_blacklist=False, perm_setting=perm_setting,
                    manage_url=manage_url, headers={'Reply-To': reply_to},
                    cc=cc, attachments=attachments)


class ReviewBase(object):

    def __init__(self, request, addon, version, review_type,
                 attachment_formset=None):
        self.request = request
        self.user = self.request.user
        self.addon = addon
        self.version = version
        self.review_type = review_type
        self.files = None
        self.comm_thread = None
        self.attachment_formset = attachment_formset
        self.in_pending = self.addon.status == amo.STATUS_PENDING
        self.in_rereview = RereviewQueue.objects.filter(
            addon=self.addon).exists()
        self.in_escalate = EscalationQueue.objects.filter(
            addon=self.addon).exists()

    def get_attachments(self):
        """
        Returns a list of triples suitable to be attached to an email.
        """
        try:
            num = int(self.attachment_formset.data['attachment-TOTAL_FORMS'])
        except (ValueError, TypeError):
            return []
        else:
            files = []
            for i in xrange(num):
                attachment_name = 'attachment-%d-attachment' % i
                attachment = self.request.FILES.get(attachment_name)
                if attachment:
                    attachment.open()
                    files.append((attachment.name, attachment.read(),
                                  attachment.content_type))
            return files

    def set_addon(self, **kw):
        """Alters addon using provided kwargs."""
        self.addon.update(_signal=False, **kw)

    def set_reviewed(self):
        """Sets reviewed timestamp on version."""
        self.version.update(_signal=False, reviewed=datetime.now())

    def set_files(self, status, files, hide_disabled_file=False):
        """Change the files to be the new status and hide as appropriate."""
        for file in files:
            file.update(_signal=False, datestatuschanged=datetime.now(),
                        reviewed=datetime.now(), status=status)
            if hide_disabled_file:
                file.hide_disabled_file()

    def create_note(self, action):
        """
        Permissions default to developers + reviewers + Mozilla contacts.
        For escalation/comment, exclude the developer from the conversation.
        """
        details = {'comments': self.data['comments'],
                   'reviewtype': self.review_type}
        if self.files:
            details['files'] = [f.id for f in self.files]

        tested = self.get_tested()  # You really should...
        if tested:
            self.data['comments'] += '\n\n%s' % tested

        # Commbadge (the future).
        note_type = comm.ACTION_MAP(action.id)
        self.comm_thread, self.comm_note = create_comm_note(
            self.addon, self.version, self.request.user,
            self.data['comments'], note_type=note_type,
            attachments=self.attachment_formset)

        # ActivityLog (ye olde).
        amo.log(action, self.addon, self.version, user=self.user,
                created=datetime.now(), details=details,
                attachments=self.attachment_formset)

    def get_tested(self):
        """
        Get string indicating device/browser used by reviewer to test.
        Will be automatically attached to the note body.
        """
        devices = self.data.get('device_types')
        browsers = self.data.get('browsers')

        if devices and browsers:
            return 'Tested on %s with %s' % (devices, browsers)
        elif devices and not browsers:
            return 'Tested on %s' % devices
        elif not devices and browsers:
            return 'Tested with %s' % browsers
        return ''

    def notify_email(self, template, subject, fresh_thread=False):
        """Notify the authors that their app has been reviewed."""
        if waffle.switch_is_active('comm-dashboard'):
            # Communication dashboard uses send_mail_comm.
            return

        data = self.data.copy()
        data.update(self.get_context_data())
        data['tested'] = self.get_tested()

        emails = list(self.addon.authors.values_list('email', flat=True))
        cc_email = self.addon.get_mozilla_contacts()

        log.info(u'Sending email for %s' % self.addon)
        send_reviewer_mail(subject % data['name'],
                           'reviewers/emails/decisions/%s.txt' % template, data,
                           emails, perm_setting='app_reviewed', cc=cc_email,
                           attachments=self.get_attachments())

    def get_context_data(self):
        # We need to display the name in some language that is relevant to the
        # recipient(s) instead of using the reviewer's. addon.default_locale
        # should work.
        if self.addon.name.locale != self.addon.default_locale:
            lang = to_language(self.addon.default_locale)
            with translation.override(lang):
                app = Webapp.objects.get(id=self.addon.id)
        else:
            app = self.addon
        return {'name': app.name,
                'reviewer': self.request.user.name,
                'detail_url': absolutify(
                    app.get_url_path()),
                'review_url': absolutify(reverse('reviewers.apps.review',
                                                 args=[app.app_slug])),
                'status_url': absolutify(app.get_dev_url('versions')),
                'comments': self.data['comments'],
                'MKT_SUPPORT_EMAIL': settings.MKT_SUPPORT_EMAIL,
                'SITE_URL': settings.SITE_URL}

    def request_information(self):
        """Send a request for information to the authors."""
        emails = list(self.addon.authors.values_list('email', flat=True))
        self.create_note(amo.LOG.REQUEST_INFORMATION)
        self.version.update(has_info_request=True)
        log.info(u'Sending request for information for %s to %s' %
                 (self.addon, emails))

        # Create thread.
        self.notify_email('info', u'More information needed to review: %s')


class ReviewApp(ReviewBase):

    def set_data(self, data):
        self.data = data
        self.files = self.version.files.all()

    def process_approve(self):
        """
        Handle the approval of apps and/or files.
        """
        if self.addon.has_incomplete_status():
            # Failsafe.
            return

        # Hold onto the status before we change it.
        status = self.addon.status
        if self.addon.publish_type == amo.PUBLISH_IMMEDIATE:
            self._process_public(amo.STATUS_PUBLIC)
        elif self.addon.publish_type == amo.PUBLISH_HIDDEN:
            self._process_public(amo.STATUS_UNLISTED)
        else:
            self._process_private()

        # Note: Post save signals shouldn't happen here. All the set_*()
        # methods pass _signal=False to prevent them from being sent. They are
        # manually triggered in the view after the transaction is committed to
        # avoid multiple indexing tasks getting fired with stale data.
        #
        # This does mean that we need to call update_version() manually to get
        # the addon in the correct state before updating names. We do that,
        # passing _signal=False again to prevent it from sending
        # 'version_changed'. The post_save() that happen in the view will
        # call it without that parameter, sending 'version_changed' normally.
        self.addon.update_version(_signal=False)
        if self.addon.is_packaged:
            self.addon.update_name_from_package_manifest()
        self.addon.update_supported_locales()
        self.addon.resend_version_changed_signal = True

        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()

        # Clear priority_review flag on approval - its not persistant.
        if self.addon.priority_review:
            self.addon.update(priority_review=False)

        # Assign reviewer incentive scores.
        return ReviewerScore.award_points(self.request.user, self.addon,
                                          status)

    def _process_private(self):
        """Make an app private."""
        if self.addon.has_incomplete_status():
            # Failsafe.
            return

        self.addon.sign_if_packaged(self.version.pk)

        # If there's only one version we set it public not matter what publish
        # type was chosen.
        if self.addon.versions.count() == 1:
            self.set_files(amo.STATUS_PUBLIC, self.version.files.all())
        else:
            self.set_files(amo.STATUS_APPROVED, self.version.files.all())

        if self.addon.status not in (amo.STATUS_PUBLIC, amo.STATUS_UNLISTED):
            self.set_addon(status=amo.STATUS_APPROVED,
                           highest_status=amo.STATUS_APPROVED)
        self.set_reviewed()

        self.create_note(amo.LOG.APPROVE_VERSION_PRIVATE)
        self.notify_email('pending_to_approved',
                          u'App approved but private: %s')

        log.info(u'Making %s approved' % self.addon)

    def _process_public(self, status):
        """Changes status to a publicly viewable status."""
        if self.addon.has_incomplete_status():
            # Failsafe.
            return

        self.addon.sign_if_packaged(self.version.pk)
        # Save files first, because set_addon checks to make sure there
        # is at least one public file or it won't make the addon public.
        self.set_files(amo.STATUS_PUBLIC, self.version.files.all())
        # If app is already an approved status, don't change it when approving
        # a version.
        if self.addon.status not in amo.WEBAPPS_APPROVED_STATUSES:
            self.set_addon(status=status, highest_status=status)
        self.set_reviewed()

        set_storefront_data.delay(self.addon.pk)

        self.create_note(amo.LOG.APPROVE_VERSION)
        if status == amo.STATUS_PUBLIC:
            self.notify_email('pending_to_public', u'App approved: %s')
        elif status == amo.STATUS_UNLISTED:
            self.notify_email('pending_to_unlisted',
                              u'App approved but unlisted: %s')

        log.info(u'Making %s public' % self.addon)

    def process_reject(self):
        """
        Reject an app.
        Changes status to Rejected.
        Creates Rejection note/email.
        """
        # Hold onto the status before we change it.
        status = self.addon.status

        self.set_files(amo.STATUS_DISABLED, self.version.files.all(),
                       hide_disabled_file=True)
        # If this app is not packaged (packaged apps can have multiple
        # versions) or if there aren't other versions with already reviewed
        # files, reject the app also.
        if (not self.addon.is_packaged or
            not self.addon.versions.exclude(id=self.version.id)
                .filter(files__status__in=amo.REVIEWED_STATUSES).exists()):
            self.set_addon(status=amo.STATUS_REJECTED)

        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()
        if self.in_rereview:
            RereviewQueue.objects.filter(addon=self.addon).delete()

        self.create_note(amo.LOG.REJECT_VERSION)
        self.notify_email('pending_to_reject',
                          u'Your submission has been rejected: %s')

        log.info(u'Making %s disabled' % self.addon)

        # Assign reviewer incentive scores.
        return ReviewerScore.award_points(self.request.user, self.addon,
                                          status, in_rereview=self.in_rereview)

    def process_escalate(self):
        """
        Ask for escalation for an app (EscalationQueue).
        Doesn't change status.
        Creates Escalation note/email.
        """
        EscalationQueue.objects.get_or_create(addon=self.addon)
        self.create_note(amo.LOG.ESCALATE_MANUAL)
        log.info(u'Escalated review requested for %s' % self.addon)
        self.notify_email('author_super_review', u'Submission Update: %s')
        log.info(u'Escalated review requested for %s' % self.addon)

        # Special senior reviewer email.
        if not waffle.switch_is_active('comm-dashboard'):
            data = self.get_context_data()
            send_reviewer_mail(u'Escalated Review Requested: %s' % data['name'],
                               'reviewers/emails/super_review.txt', data,
                               [settings.MKT_SENIOR_EDITORS_EMAIL],
                               attachments=self.get_attachments())

    def process_comment(self):
        """
        Editor comment (not visible to developer).
        Doesn't change status.
        Creates Reviewer Comment note/email.
        """
        self.version.update(has_editor_comment=True)
        self.create_note(amo.LOG.COMMENT_VERSION)

    def process_clear_escalation(self):
        """
        Clear app from escalation queue.
        Doesn't change status.
        Doesn't create note/email.
        """
        EscalationQueue.objects.filter(addon=self.addon).delete()
        self.create_note(amo.LOG.ESCALATION_CLEARED)
        log.info(u'Escalation cleared for app: %s' % self.addon)

    def process_clear_rereview(self):
        """
        Clear app from re-review queue.
        Doesn't change status.
        Doesn't create note/email.
        """
        RereviewQueue.objects.filter(addon=self.addon).delete()
        self.create_note(amo.LOG.REREVIEW_CLEARED)
        log.info(u'Re-review cleared for app: %s' % self.addon)
        # Assign reviewer incentive scores.
        return ReviewerScore.award_points(self.request.user, self.addon,
                                          self.addon.status, in_rereview=True)

    def process_disable(self):
        """
        Bans app from Marketplace, clears app from all queues.
        Changes status to Disabled.
        Creates Banned/Disabled note/email.
        """
        if not acl.action_allowed(self.request, 'Apps', 'Edit'):
            return

        # Disable disables all files, not just those in this version.
        self.set_files(amo.STATUS_DISABLED,
                       File.objects.filter(version__addon=self.addon),
                       hide_disabled_file=True)
        self.addon.update(status=amo.STATUS_DISABLED)
        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()
        if self.in_rereview:
            RereviewQueue.objects.filter(addon=self.addon).delete()

        set_storefront_data.delay(self.addon.pk, disable=True)

        self.notify_email('disabled', u'App banned by reviewer: %s')
        self.create_note(amo.LOG.APP_DISABLED)
        log.info(u'App %s has been banned by a reviewer.' % self.addon)


class ReviewHelper(object):
    """
    A class that builds enough to render the form back to the user and
    process off to the correct handler.
    """

    def __init__(self, request=None, addon=None, version=None,
                 attachment_formset=None):
        self.handler = None
        self.required = {}
        self.addon = addon
        self.version = version
        self.all_files = version and version.files.all()
        self.attachment_formset = attachment_formset
        self.get_review_type(request, addon, version)
        self.actions = self.get_actions()

    def set_data(self, data):
        self.handler.set_data(data)

    def get_review_type(self, request, addon, version):
        if EscalationQueue.objects.filter(addon=addon).exists():
            queue = 'escalated'
        elif RereviewQueue.objects.filter(addon=addon).exists():
            queue = 'rereview'
        else:
            queue = 'pending'
        self.review_type = queue
        self.handler = ReviewApp(request, addon, version, queue,
                                 attachment_formset=self.attachment_formset)

    def get_actions(self):
        """Get the appropriate handler based on the action."""
        public = {
            'method': self.handler.process_approve,
            'minimal': False,
            'label': _lazy(u'Approve'),
            'details': _lazy(u'This will approve the app and allow the '
                             u'author(s) to publish it.')}
        reject = {
            'method': self.handler.process_reject,
            'label': _lazy(u'Reject'),
            'minimal': False,
            'details': _lazy(u'This will reject the app, remove it from '
                             u'the review queue and un-publish it if already '
                             u'published.')}
        info = {
            'method': self.handler.request_information,
            'label': _lazy(u'Request more information'),
            'minimal': True,
            'details': _lazy(u'This will send the author(s) an email '
                             u'requesting more information.')}
        escalate = {
            'method': self.handler.process_escalate,
            'label': _lazy(u'Escalate'),
            'minimal': True,
            'details': _lazy(u'Flag this app for an admin to review. The '
                             u'comments are sent to the admins, '
                             u'not the author(s).')}
        comment = {
            'method': self.handler.process_comment,
            'label': _lazy(u'Comment'),
            'minimal': True,
            'details': _lazy(u'Make a comment on this app.  The author(s) '
                             u'won\'t be able to see these comments.')}
        clear_escalation = {
            'method': self.handler.process_clear_escalation,
            'label': _lazy(u'Clear Escalation'),
            'minimal': True,
            'details': _lazy(u'Clear this app from the escalation queue. The '
                             u'author(s) will get no email or see comments '
                             u'here.')}
        clear_rereview = {
            'method': self.handler.process_clear_rereview,
            'label': _lazy(u'Clear Re-review'),
            'minimal': True,
            'details': _lazy(u'Clear this app from the re-review queue. The '
                             u'author(s) will get no email or see comments '
                             u'here.')}
        disable = {
            'method': self.handler.process_disable,
            'label': _lazy(u'Ban app'),
            'minimal': True,
            'details': _lazy(u'Ban the app from Marketplace. Similar to Reject '
                             u'but the author(s) can\'t resubmit. To only be '
                             u'used in extreme cases.')}

        actions = SortedDict()

        if not self.version:
            # Return early if there is no version, this app is incomplete.
            actions['info'] = info
            actions['comment'] = comment
            return actions

        file_status = self.version.files.values_list('status', flat=True)
        multiple_versions = (File.objects.exclude(version=self.version)
                                         .filter(
                                             version__addon=self.addon,
                                             status__in=amo.REVIEWED_STATUSES)
                                         .exists())

        show_privileged = (not self.version.is_privileged
                           or acl.action_allowed(self.handler.request, 'Apps',
                                                 'ReviewPrivileged'))

        # Public.
        if ((self.addon.is_packaged and amo.STATUS_PUBLIC not in file_status
             and show_privileged)
            or (not self.addon.is_packaged and
                self.addon.status != amo.STATUS_PUBLIC)):
            actions['public'] = public

        # Reject.
        if self.addon.is_packaged and show_privileged:
            # Packaged apps reject the file only, or the app itself if there's
            # only a single version.
            if (not multiple_versions and
                self.addon.status not in [amo.STATUS_REJECTED,
                                          amo.STATUS_DISABLED]):
                actions['reject'] = reject
            elif multiple_versions and amo.STATUS_DISABLED not in file_status:
                actions['reject'] = reject
        elif not self.addon.is_packaged:
            # Hosted apps reject the app itself.
            if self.addon.status not in [amo.STATUS_REJECTED,
                                         amo.STATUS_DISABLED]:
                actions['reject'] = reject

        # Ban/Disable.
        if (acl.action_allowed(self.handler.request, 'Apps', 'Edit') and (
                self.addon.status != amo.STATUS_DISABLED or
                amo.STATUS_DISABLED not in file_status)):
            actions['disable'] = disable

        # Clear escalation.
        if self.handler.in_escalate:
            actions['clear_escalation'] = clear_escalation

        # Clear re-review.
        if self.handler.in_rereview:
            actions['clear_rereview'] = clear_rereview

        # Escalate.
        if not self.handler.in_escalate:
            actions['escalate'] = escalate

        # Request info and comment are always shown.
        actions['info'] = info
        actions['comment'] = comment

        return actions

    def process(self):
        """Call handler."""
        action = self.handler.data.get('action', '')
        if not action:
            raise NotImplementedError
        return self.actions[action]['method']()


def clean_sort_param(request, date_sort='created'):
    """
    Handles empty and invalid values for sort and sort order
    'created' by ascending is the default ordering.
    """
    sort = request.GET.get('sort', date_sort)
    order = request.GET.get('order', 'asc')

    if sort not in ('name', 'created', 'nomination'):
        sort = date_sort
    if order not in ('desc', 'asc'):
        order = 'asc'
    return sort, order


def create_sort_link(pretty_name, sort_field, get_params, sort, order):
    """Generate table header sort links.

    pretty_name -- name displayed on table header
    sort_field -- name of the sort_type GET parameter for the column
    get_params -- additional get_params to include in the sort_link
    sort -- the current sort type
    order -- the current sort order
    """
    get_params.append(('sort', sort_field))

    if sort == sort_field and order == 'asc':
        # Have link reverse sort order to desc if already sorting by desc.
        get_params.append(('order', 'desc'))
    else:
        # Default to ascending.
        get_params.append(('order', 'asc'))

    # Show little sorting sprite if sorting by this field.
    url_class = ''
    if sort == sort_field:
        url_class = ' class="sort-icon ed-sprite-sort-%s"' % order

    return u'<a href="?%s"%s>%s</a>' % (urllib.urlencode(get_params, True),
                                        url_class, pretty_name)


class AppsReviewing(object):
    """
    Class to manage the list of apps a reviewer is currently reviewing.

    Data is stored in memcache.
    """

    def __init__(self, request):
        self.request = request
        self.user_id = request.user.id
        self.key = '%s:myapps:%s' % (settings.CACHE_PREFIX, self.user_id)

    def get_apps(self):
        ids = []
        my_apps = cache.get(self.key)
        if my_apps:
            for id in my_apps.split(','):
                valid = cache.get(
                    '%s:review_viewing:%s' % (settings.CACHE_PREFIX, id))
                if valid and valid == self.user_id:
                    ids.append(id)

        apps = []
        for app in Webapp.objects.filter(id__in=ids):
            apps.append({
                'app': app,
                'app_attrs': json.dumps(
                    product_as_dict(self.request, app, False, 'reviewer'),
                    cls=JSONEncoder),
            })
        return apps

    def add(self, addon_id):
        my_apps = cache.get(self.key)
        if my_apps:
            apps = my_apps.split(',')
        else:
            apps = []
        apps.append(addon_id)
        cache.set(self.key, ','.join(map(str, set(apps))),
                  amo.EDITOR_VIEWING_INTERVAL * 2)


def device_queue_search(request):
    """
    Returns a queryset that can be used as a base for searching the device
    specific queue.
    """
    filters = {
        'status': amo.STATUS_PENDING,
        'disabled_by_user': False,
    }
    sig = request.GET.get('pro')
    if sig:
        profile = FeatureProfile.from_signature(sig)
        filters.update(dict(
            **profile.to_kwargs(prefix='_latest_version__features__has_')
        ))
    return Webapp.version_and_file_transformer(
        Webapp.objects.filter(**filters))


def log_reviewer_action(addon, user, msg, action, **kwargs):
    create_comm_note(addon, addon.latest_version, user, msg,
                     note_type=comm.ACTION_MAP(action.id))
    amo.log(action, addon, addon.latest_version, details={'comments': msg},
            **kwargs)


class ReviewersQueuesHelper(object):
    def __init__(self, request=None):
        self.request = request

    @amo.cached_property
    def excluded_ids(self):
        # We need to exclude Escalated Apps from almost all queries, so store
        # the result once.
        return self.get_escalated_queue().values_list('addon', flat=True)

    def get_escalated_queue(self):
        return EscalationQueue.objects.no_cache().filter(
            addon__disabled_by_user=False)

    def get_pending_queue(self):
        return (Version.objects.no_cache().filter(
            files__status=amo.STATUS_PENDING,
            addon__disabled_by_user=False,
            addon__status=amo.STATUS_PENDING)
            .exclude(addon__id__in=self.excluded_ids)
            .order_by('nomination', 'created')
            .select_related('addon', 'files').no_transforms())

    def get_rereview_queue(self):
        return (RereviewQueue.objects.no_cache()
            .filter(addon__disabled_by_user=False)
            .exclude(addon__in=self.excluded_ids))

    def get_updates_queue(self):
        return (Version.objects.no_cache().filter(
            # Note: this will work as long as we disable files of existing
            # unreviewed versions when a new version is uploaded.
            files__status=amo.STATUS_PENDING,
            addon__disabled_by_user=False,
            addon__is_packaged=True,
            addon__status__in=amo.WEBAPPS_APPROVED_STATUSES)
            .exclude(addon__id__in=self.excluded_ids)
            .order_by('nomination', 'created')
            .select_related('addon', 'files').no_transforms())

    def get_moderated_queue(self):
        return (Review.objects.no_cache()
                .exclude(Q(addon__isnull=True) | Q(reviewflag__isnull=True))
                .exclude(addon__status=amo.STATUS_DELETED)
                .filter(editorreview=True)
                .order_by('reviewflag__created'))

    def sort(self, qs, date_sort='created'):
        """Given a queue queryset, return the sorted version."""
        if qs.model == Webapp:
            return self._do_sort_webapp(qs, date_sort)
        return self._do_sort_queue_obj(qs, date_sort)

    def _do_sort_webapp(self, qs, date_sort):
        """
        Column sorting logic based on request GET parameters.
        """
        sort_type, order = clean_sort_param(self.request, date_sort=date_sort)
        order_by = ('-' if order == 'desc' else '') + sort_type

        # Sort.
        if sort_type == 'name':
            # Sorting by name translation.
            return order_by_translation(qs, order_by)

        else:
            return qs.order_by('-priority_review', order_by)

    def _do_sort_queue_obj(self, qs, date_sort):
        """
        Column sorting logic based on request GET parameters.
        Deals with objects with joins on the Addon (e.g. RereviewQueue, Version).
        Returns qs of apps.
        """
        sort_type, order = clean_sort_param(self.request, date_sort=date_sort)
        sort_str = sort_type

        if sort_type not in [date_sort, 'name']:
            sort_str = 'addon__' + sort_type

        # sort_str includes possible joins when ordering.
        # sort_type is the name of the field to sort on without desc/asc markers.
        # order_by is the name of the field to sort on with desc/asc markers.
        order_by = ('-' if order == 'desc' else '') + sort_str

        # Sort.
        if sort_type == 'name':
            # Sorting by name translation through an addon foreign key.
            return order_by_translation(
                Webapp.objects.filter(id__in=qs.values_list('addon', flat=True)),
                order_by)

        # Convert sorted queue object queryset to sorted app queryset.
        sorted_app_ids = (qs.order_by('-addon__priority_review', order_by)
                            .values_list('addon', flat=True))
        qs = Webapp.objects.filter(id__in=sorted_app_ids)
        return manual_order(qs, sorted_app_ids, 'addons.id')
