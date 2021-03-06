import csp.views
from waffle.views import wafflejs

from django.conf.urls import include, patterns, url
from django.views.decorators.cache import never_cache

from . import views


services_patterns = patterns('',
    url('^monitor(.json)?$', never_cache(views.monitor),
        name='amo.monitor'),
    url('^loaded$', never_cache(views.loaded), name='amo.loaded'),
    url('^csp/policy$', csp.views.policy, name='amo.csp.policy'),
    url('^csp/report$', views.cspreport, name='amo.csp.report'),
    url('^timing/record$', views.record, name='amo.timing.record'),
)

urlpatterns = patterns('',
    url(r'^wafflejs$', wafflejs, name='wafflejs'),
    ('^services/', include(services_patterns)),
)
