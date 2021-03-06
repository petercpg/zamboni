from django.conf.urls import include, patterns, url

import amo
from mkt.receipts.urls import receipt_patterns
from mkt.reviewers import views


# All URLs under /reviewers/.
url_patterns = patterns('',
    url(r'^apps/$', views.home, name='reviewers.home'),
    url(r'^$', views.route_reviewer, name='reviewers'),
    url(r'^apps/queue/$', views.queue_apps,
        name='reviewers.apps.queue_pending'),
    url(r'^apps/queue/region/(?P<region>[^ /]+)?$', views.queue_region,
        name='reviewers.apps.queue_region'),
    url(r'^apps/queue/rereview/$', views.queue_rereview,
        name='reviewers.apps.queue_rereview'),
    url(r'^apps/queue/updates/$', views.queue_updates,
        name='reviewers.apps.queue_updates'),
    url(r'^apps/queue/escalated/$', views.queue_escalated,
        name='reviewers.apps.queue_escalated'),
    url(r'^apps/queue/moderated/$', views.queue_moderated,
        name='reviewers.apps.queue_moderated'),
    url(r'^apps/queue/device/$', views.queue_device,
        name='reviewers.apps.queue_device'),
    url(r'^apps/review/%s$' % amo.APP_SLUG, views.app_review,
        name='reviewers.apps.review'),
    url(r'^apps/review/%s/manifest$' % amo.APP_SLUG, views.app_view_manifest,
        name='reviewers.apps.review.manifest'),
    url(r'^apps/review/attachment/(\d+)$', views.attachment,
        name='reviewers.apps.review.attachment'),
    url(r'^apps/review/%s/abuse$' % amo.APP_SLUG, views.app_abuse,
        name='reviewers.apps.review.abuse'),
    url(r'^apps/logs$', views.logs, name='reviewers.apps.logs'),
    url(r'^apps/motd$', views.motd, name='reviewers.apps.motd'),
    url(r'^queue_viewing$', views.queue_viewing,
        name='reviewers.queue_viewing'),
    url(r'^review_viewing$', views.review_viewing,
        name='reviewers.review_viewing'),
    url(r'^apps/reviewing$', views.apps_reviewing,
        name='reviewers.apps.apps_reviewing'),

    url(r'^receipt/', include(receipt_patterns)),
    url(r'^%s/(?P<version_id>\d+)/mini-manifest$' % amo.APP_SLUG,
        views.mini_manifest, name='reviewers.mini_manifest'),
    url(r'^signed/%s/(?P<version_id>\d+)$' % amo.APP_SLUG,
        views.get_signed_packaged, name='reviewers.signed'),

    url(r'''^performance/(?P<username>[^/<>"']+)?$''', views.performance,
        name='reviewers.performance'),
    url(r'^leaderboard/$', views.leaderboard, name='reviewers.leaderboard'),
)

api_patterns = patterns('',
    url('^reviewers/search', views.ReviewersSearchView.as_view(),
        name='reviewers-search-api'),
    url(r'^reviewers/app/(?P<pk>[^/<>"\']+)/approve/(?P<region>[^ /]+)?$',
        views.ApproveRegion.as_view(), name='approve-region'),
    url(r'^reviewers/reviewing', views.ReviewingView.as_view(),
        name='reviewing-list'),
    url('^reviewers/(?P<addon_slug>[\w-]+)/review/(?P<review_pk>\d+)/translate'
        '/(?P<language>[a-z]{2}(-[A-Z]{2})?)$',
        views.review_translate,
        name='reviewers.review_translate'),
    url(r'^reviewers/app/(?P<pk>[^/<>"\']+)/token$',
        views.GenerateToken.as_view(), name='generate-reviewer-token'),
)
