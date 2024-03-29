# -*- coding: utf-8 -*-

import json

from django.shortcuts import render, redirect
from django.template import RequestContext
from django.contrib.gis.geos import fromstr
from ipware.ip import get_ip
from django.http import HttpResponse, Http404
from django.core.cache import cache
from django.views.decorators.cache import cache_page
import django.db.models
import geojson
from django.conf import settings
import requests

from content import models, utils


CACHE_LENGTH = 60 * 15

LOCATIONS = (   ('lesvos','Lesvos'),
                ('kos', 'Kos'),
                ('athens', 'Athens'),
                ('gevgelija', 'Gevgelija'),
                ('tabanovce', 'Tabanovce/Preševo')
            )


def landing(request):
    ip_position = location_best_guess(request)
    point = 'POINT({} {})'.format(ip_position['longitude'], ip_position['latitude'])
    geopoint = fromstr(point, srid=4326)

    current_location = {}

    location = models.Location.objects.filter(area__intersects=geopoint, enabled=True).order_by('-parent')
    languages = list(models.Language.objects.all().order_by('name'))

    if location:
        found_location = location[0] if location else None

        location_content = {}
        for c in found_location.content.all():
            location_content[c.language.iso_code] = {
                "title": c.title,
                "html_url": c.html_url
            }
        current_location = {
            "id": found_location.id,
            "name": found_location.name,
            "slug": found_location.slug,
            "contents": location_content
        }

    return render(request, 'landing.html', context={
        "current_location": json.dumps(current_location),
        "languages": languages,
        "locations": LOCATIONS
    }, context_instance=RequestContext(request))


def site_map(request):
    query = models.Location.objects.filter(enabled=True,
                                           country__isnull=False)
    query = query.values('country').annotate(count=django.db.models.Count('*'))

    countries = dict([(c['country'].lower(), c['count']) for c in query])
    best_guess = location_best_guess(request, timeout=2)
    return render(request,
                  "site-map.html",
                  {
                      "available_countries": json.dumps(countries),
                      "location": best_guess,
                      "languages": json.dumps(
                          [dict(iso_code=a.iso_code, name=a.name, id=a.id) for a in models.Language.objects.all()]),
                  },
                  RequestContext(request))


@cache_page(CACHE_LENGTH)
def index(request, page_id, language):
    location = models.Location.objects.filter(id=page_id)
    html_content = ""

    if location:
        location = location[0]

        if location.managed_locally:
            """
            Loading content from a CMS or CMS-like site
            """
            url_parts = [location.slug]
            m = location
            while m.parent:
                url_parts.append(m.parent.slug)
                m = m.parent

            complete_url = '/'.join(reversed(url_parts))

            cms_url = utils.get_cms_url(language, complete_url)

            if cms_url in cache:
                html_content = cache.get(cms_url)
            else:
                html_content = utils.get_cms_page(language, complete_url)
                cache.set(cms_url, html_content)
        else:
            """
            Loading content from a google doc
            """
            html_content = ""

            content = location.content.filter(language__iso_code=language)

            default_content = location.content.all()
            content = content[0] if content else default_content[0] if default_content else None

            if content and content.html_url:
                doc_path = content.html_url
                cached = cache.get(doc_path)
                if cached:
                    html_content = cached
                else:
                    html_content = requests.get(doc_path).text

    languages = list(models.Language.objects.all().order_by('name'))

    return render(request, 'index.html', context={
        "html_content": html_content,
        "languages": languages,
        "location": location,
        "service_map_enabled": settings.ENABLE_SERVICES or False,
    }, context_instance=RequestContext(request))


def depth(location):
    d = 0
    parent = location.parent
    while parent:
        d += 1
        parent = parent.parent

    return d


def location_from_device(request):
    lnglat = request.GET['lnglat']
    point = 'POINT({})'.format(lnglat)
    geopoint = fromstr(point, srid=4326)

    location = sorted(models.Location.objects.filter(area__intersects=geopoint, enabled=True), key=lambda x: -depth(x))

    if not location:
        raise Http404()

    found_location = location[0] if location else None

    location_content = {}
    for c in found_location.content.all():
        location_content[c.language.iso_code] = {
            "title": c.title,
            "html_url": c.html_url
        }

    return HttpResponse(json.dumps({
        "id": found_location.id,
        "name": found_location.name,
        "slug": found_location.slug,
        "contents": location_content
    }), content_type='application/json')


def location_best_guess(request, timeout=0.2):
    latitude = 0
    longitude = 0
    country_code = None
    try:
        location_response = requests.get('http://ip-api.com/json/{}'.format(get_ip(request)), timeout=timeout)

        location_info = location_response.json()

        if location_info['status'] == 'success':
            latitude = location_info['lat']
            longitude = location_info['lon']
            if 'countryCode' in location_info:
                country_code = location_info['countryCode'].lower()
    except Exception as e:
        pass

    return {"latitude": latitude, "longitude": longitude, 'countryCode': country_code}


def slug_no_language(request, slug):
    if 'HTTP_ACCEPT_LANGUAGE' in request.META:
        accept_language = request.META['HTTP_ACCEPT_LANGUAGE'].split(',')
        first_language = accept_language[0].split('-')

        if first_language:
            first_language = first_language[0]
    else:
        first_language = 'en'

    return slug_index(request, slug, first_language)


def slug_index(request, slug, language):
    locations = models.Location.objects.filter(slug=slug)

    if not locations:
        return redirect('/')

    location = locations[0]

    return cache_page(CACHE_LENGTH)(index)(request, location.id, language)


def services(request, slug, service_category=None):
    if 'HTTP_ACCEPT_LANGUAGE' in request.META:
        accept_language = request.META['HTTP_ACCEPT_LANGUAGE'].split(',')
        first_language = accept_language[0].split('-')

        if first_language:
            first_language = first_language[0]
    else:
        first_language = 'en'

    location = models.Location.objects.filter(slug=slug)

    if location:
        location = location[0]
    else:
        return redirect('/')

    extent = geojson.Polygon(([(a[0], a[1], 0) for a in location.area.shell.array],))

    print extent

    return render(request,
                  "service-map.html",
                  {
                      "extent": unicode(extent),
                      "slug": location.slug,
                  },
                  RequestContext(request))


def acknowledgements(request):
    return render(request, "acknowledgments.html", {}, RequestContext(request))
