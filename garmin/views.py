"""HTTP views for OAuth + webhook notifications.

The OAuth views (``connect`` / ``callback`` / ``disconnect``) cover the
web-callback flow used in admin / dev / testing. Mobile clients should POST
tokens (or an auth code) to a project-local endpoint that calls
:func:`garmin.oauth.ingest_tokens` or :func:`garmin.oauth.exchange_code`.

The ``notification_receiver`` view accepts Garmin push/ping POSTs and emits a
:data:`garmin.signals.notification_received` signal for every parseable body.
Point every summary type's webhook URL in the Garmin developer portal at this
one endpoint — the payload keys identify the type.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import oauth
from .models import GarminConnection
from .signals import notification_received

SESSION_KEY = "garmin_oauth_flow"


def _success_url() -> str:
    return getattr(settings, "GARMIN_CONNECT_SUCCESS_URL", "/admin/")


@login_required
@require_http_methods(["GET"])
def connect(request: HttpRequest) -> HttpResponse:
    auth_url, flow_state = oauth.build_authorization_url()
    request.session[SESSION_KEY] = flow_state.model_dump()
    return redirect(auth_url)


@login_required
@require_http_methods(["GET"])
def callback(request: HttpRequest) -> HttpResponse:
    code = request.GET.get("code")
    received_state = request.GET.get("state")
    error = request.GET.get("error")
    if error:
        return HttpResponseBadRequest(f"OAuth error: {error}")
    if not code:
        return HttpResponseBadRequest("Missing authorization code")

    stashed = request.session.pop(SESSION_KEY, None)
    if not stashed:
        return HttpResponseBadRequest("No OAuth flow in progress")
    flow_state = oauth.OAuthFlowState.model_validate(stashed)

    try:
        tokens = oauth.exchange_code(
            code=code,
            code_verifier=flow_state.code_verifier,
            expected_state=flow_state.state,
            received_state=received_state,
        )
    except oauth.StateMismatchError:
        return HttpResponseBadRequest("OAuth state mismatch")

    oauth.ingest_tokens(customer=request.user, tokens=tokens)
    return redirect(_success_url())


@login_required
@require_POST
def disconnect(request: HttpRequest) -> HttpResponse:
    try:
        connection = GarminConnection.objects.get(customer=request.user)
    except GarminConnection.DoesNotExist:
        return redirect(_success_url())
    oauth.revoke(connection)
    return redirect(_success_url())


@csrf_exempt
@require_POST
def notification_receiver(request: HttpRequest) -> HttpResponse:
    """Receive Garmin push/ping webhook POSTs.

    Garmin sends no verification handshake or auth header; it just expects a
    fast 200 (endpoints that keep failing get disabled). Validate the JSON,
    emit ``notification_received``, and return 200. Any heavy lifting belongs
    in the signal handler (which should hand off to a queue).
    """
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("invalid JSON body")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("expected a JSON object")

    notification_received.send(sender=None, payload=payload)
    return HttpResponse(status=200)
