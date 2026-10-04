"""Authenticated settings and allowlisted frontend diagnostic collection."""
from __future__ import annotations

from .diagnostics import MAX_CLIENT_EVENTS
from .routing import authed


@authed
def get_diagnostics(app, req, mesh):
    return app.diagnostics.configuration()


@authed
def set_diagnostics(app, req, mesh):
    enabled = req.data.get('enabled')
    if type(enabled) is not bool:
        return {'error': 'enabled must be a boolean'}
    if not app.diagnostics.set_enabled(enabled, slow_ms=req.data.get('slow_ms'),
                                       sample_rate=req.data.get('sample_rate')):
        return {'error': 'diagnostics setting unavailable'}
    return app.diagnostics.configuration()


@authed
def collect_events(app, req, mesh):
    events = req.data.get('events')
    accepted, dropped = app.diagnostics.collect(events)
    return {'ok': True, 'accepted': accepted, 'dropped': dropped,
            'max_events': MAX_CLIENT_EVENTS}


GET = {'/api/diagnostics': get_diagnostics}
POST = {'/api/diagnostics': set_diagnostics,
        '/api/diagnostics/events': collect_events}
