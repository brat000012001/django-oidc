# coding: utf-8

import sys
import logging
from urllib.parse import parse_qs
from wsgiref.util import is_hop_by_hop

from django.conf import settings
from django.contrib.auth import logout as auth_logout, authenticate, login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView as auth_login_view, LogoutView as auth_logout_view
from django.shortcuts import redirect, render, resolve_url
from django.http import HttpResponse
from django import forms
from django.template import RequestContext
from oic.oic.message import IdToken

from djangooidc.oidc import OIDCClients, OIDCError

logger = logging.getLogger(__name__)

CLIENTS = OIDCClients(settings)


# Step 1: provider choice (form). Also - Step 2: redirect to OP. (Step 3 is OP business.)
class DynamicProvider(forms.Form):
    hint = forms.CharField(required=True, label='OpenID Connect full login', max_length=250)


def openid(request, op_name=None):
    client = None
    request.session["next"] = request.GET["next"] if "next" in request.GET.keys() else "/"
    try:
        dyn = settings.OIDC_ALLOW_DYNAMIC_OP or False
    except:
        dyn = True

    try:
        template_name = settings.OIDC_LOGIN_TEMPLATE
    except AttributeError:
        template_name = 'djangooidc/login.html'

    # Internal login?
    if request.method == 'POST' and "internal_login" in request.POST:
        ilform = AuthenticationForm(request.POST)
        return auth_login_view(request)
    else:
        ilform = AuthenticationForm()

    # Try to find an OP client either from the form or from the op_name URL argument
    if request.method == 'GET' and op_name is not None:
        client = CLIENTS[op_name]
        request.session["op"] = op_name

    if request.method == 'POST' and dyn:
        form = DynamicProvider(request.POST)
        if form.is_valid():
            try:
                client = CLIENTS.dynamic_client(form.cleaned_data["hint"])
                request.session["op"] = client.provider_info["issuer"]
            except Exception as e:
                logger.exception("could not create OOID client")
                return render(request, "djangooidc/error.html", {"error": e})
    else:
        form = DynamicProvider()

    # If we were able to determine the OP client, just redirect to it with an authentication request
    if client:
        try:
            return client.create_authn_request(request.session)
        except Exception as e:
            return render(request, "djangooidc/error.html", {"error": e})

    # Otherwise just render the list+form.
    return render(request, template_name,
                              {"op_list": [i for i in settings.OIDC_PROVIDERS.keys() if i], 'dynamic': dyn,
                               'form': form, 'ilform': ilform, "next": request.session["next"]})


# Step 4: analyze the token returned by the OP
def authz_cb(request):
    client = CLIENTS[request.session["op"]]
    query = None

    try:
        query = parse_qs(request.META['QUERY_STRING'])
        userinfo = client.callback(query, request.session)
        request.session["userinfo"] = userinfo
        user = authenticate(**userinfo)
        if user:
            login(request, user)
            return redirect(request.session["next"])
        else:
            raise Exception('this login is not valid in this application')
    except OIDCError as e:
        return render(request, "djangooidc/error.html", {"error": e, "callback": query})


def logout(request, next_page=None):
    if not "op" in request.session.keys():
        return auth_logout_view(request, next_page)

    client = CLIENTS[request.session["op"]]

    # User is by default NOT redirected to the app - it stays on an OP page after logout.
    # Here we determine if a redirection to the app was asked for and is possible.
    if next_page is None and "next" in request.GET.keys():
        next_page = request.GET['next']
    if next_page is None and "next" in request.session.keys():
        next_page = request.session['next']
    extra_args = {}
    if "post_logout_redirect_uris" in client.registration_response.keys() and len(
            client.registration_response["post_logout_redirect_uris"]) > 0:
        if next_page is not None:
            # First attempt a direct redirection from OP to next_page
            next_page_url = resolve_url(next_page)
            urls = [url for url in client.registration_response["post_logout_redirect_uris"] if next_page_url in url]
            if len(urls) > 0:
                extra_args["post_logout_redirect_uri"] = urls[0]
            else:
                # It is not possible to directly redirect from the OP to the page that was asked for.
                # We will try to use the redirection point - if the redirection point URL is registered that is.
                next_page_url = resolve_url('openid_logout_cb')
                urls = [url for url in client.registration_response["post_logout_redirect_uris"] if
                        next_page_url in url]
                if len(urls) > 0:
                    extra_args["post_logout_redirect_uri"] = urls[0]
                else:
                    # Just take the first registered URL as a desperate attempt to come back to the application
                    extra_args["post_logout_redirect_uri"] = client.registration_response["post_logout_redirect_uris"][
                        0]
    else:
        # No post_logout_redirect_uris registered at the OP - no redirection to the application is possible anyway
        pass

    # Redirect client to the OP logout page
    try:
        request_args = None
        if 'id_token_raw' in request.session.keys():
            logger.info('logout => found id_token_raw: %s' % request.session['id_token_raw'])
            request_args = {'id_token_hint': request.session['id_token_raw']}
        res = client.do_end_session_request(state=request.session["state"],
                                            extra_args=extra_args, request_args=request_args)

        logger.debug('********  do_end_session_request ********** status: %s, headers: %s' % (str(res.status_code),str(res.headers))) 

        # a workaround to avoid an exception if 'content-type' header is absent 
        # (e.g. if the server is behind a reverse rpxoy)
        if 'content-type' in res.headers:
            content_type = res.headers['content-type']
        else:
            # TODO: what the default content-type should be?
            content_type = 'text/plain' 
        resp = HttpResponse(content_type=content_type, status=res.status_code, content=res._content)

        # Check for hop-by-hop headers to prevent WSGI application errors thrown later in the pipeline
        for key, val in res.headers.items():
            if is_hop_by_hop(key): continue
            resp[key] = val
        return resp
    except:
        # Handle the responses from the server that cannot be parsed by oic/oauth2 client, e.g. error responses
        # the server generates if the session has expired
        logger.debug('************ logout failed ***************: %s' % sys.exc_info()[0]) 
        resp = HttpResponse(content_type='text/plain', status=302)
        resp['Location'] = '/'
        return resp
    finally:
        # Always remove Django session stuff - even if not logged out from OP. Don't wait for the callback as it may never come.
        auth_logout(request)
        if next_page:
            request.session['next'] = next_page


def logout_cb(request):
    """ Simple redirection view: after logout, just redirect to a parameter value inside the session """
    next = request.session["next"] if "next" in request.session.keys() else "/"
    return redirect(next)
