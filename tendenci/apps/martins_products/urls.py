from django.conf.urls.defaults import patterns, url
from tendenci.apps.martins_products.feeds import LatestEntriesFeed

urlpatterns = patterns('tendenci.apps.martins_products.views',
    url(r'^products/$', 'index', name="products"),
    url(r'^products/search/$', 'search', name="products.search"),
    url(r'^products/feed/$', LatestEntriesFeed(), name='products.feed'),
    url(r'^products/(?P<slug>[\w\-]+)/$', 'detail', name="products.detail"),
        url(r'^products/category/(?P<id>\d+)/$', 'category', name="products.category"),
)