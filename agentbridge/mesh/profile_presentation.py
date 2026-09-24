"""Request-local profile decorations from captured accounts, never a directory walk.

The caller owns current account, selected-room membership and final source cuts.
Unknown MEMBERS/AGENTS reachability defers that private field rather than
pretending this selected room proves the absence of another relationship.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.models import Account, Audience, UserKind
from ..store.presence_index import PresenceDisplayInputs


@dataclass(frozen=True)
class ProfilePresentation:
    profile: dict
    pending_fields: tuple[str, ...]


@dataclass(frozen=True)
class PresencePresentation:
    values: dict
    pending: bool


def _text(value, limit, label):
    if type(value) is not str or len(value.encode()) > limit:
        raise ValueError(f'invalid {label}')
    return value


def project_profile(account: Account, viewer: str, *, viewer_kind: UserKind,
                    same_selected_room: bool | None = None,
                    viewer_owns_agent: bool | None = None) -> ProfilePresentation:
    """Evaluate one captured account under only positively proved relations."""
    if (type(account) is not Account or type(viewer_kind) is not UserKind
            or type(same_selected_room) not in (bool, type(None))
            or type(viewer_owns_agent) not in (bool, type(None))):
        raise ValueError('invalid profile inputs')
    _text(viewer, 256, 'viewer')
    _text(account.name, 256, 'account name')
    p = account.privacy
    own = viewer == account.name or (account.kind is UserKind.AGENT
                                      and account.agent is not None
                                      and account.agent.owner == viewer)

    def allowed(audience):
        if own or audience is Audience.EVERYONE:
            return True
        if audience is Audience.NOBODY:
            return False
        if audience is Audience.MEMBERS:
            return True if same_selected_room is True else None
        if audience is Audience.AGENTS:
            return True if viewer_kind is UserKind.AGENT or viewer_owns_agent is True else None
        raise ValueError('invalid profile audience')

    result = {
        'name': account.name, 'kind': account.kind.value,
        'display': _text(account.display, 256, 'display'),
        'active': bool(account.active),
        'messaging': p.messaging.value, 'add_to_group': p.add_to_group.value,
    }
    if account.kind is UserKind.AGENT and account.agent is not None:
        result['owner'] = _text(account.agent.owner, 256, 'owner')
        result['machine'] = _text(account.agent.machine, 256, 'machine')
    pending = []
    for name in ('about', 'status', 'photo', 'last_seen', 'online'):
        decision = allowed(getattr(p, name))
        if decision is None:
            pending.append(name)
        if name == 'about' and decision:
            result['about'] = _text(account.about, 4096, 'about')
        elif name == 'status' and decision:
            result['status'] = {
                'state': _text(account.status.state, 128, 'status state'),
                'text': _text(account.status.text, 1024, 'status text'),
            }
        elif name == 'photo':
            result['photo_visible'] = decision
        elif name == 'last_seen':
            result['may_see_last_seen'] = decision
        elif name == 'online':
            result['may_see_online'] = decision
    return ProfilePresentation(result, tuple(pending))


def project_presence(inputs: PresenceDisplayInputs, user: str, profile: ProfilePresentation,
                     *, now_ns: int, stale_s: float, max_observation_s: float = 30.0,
                     ) -> PresencePresentation:
    """Clock/visibility projection of one exact raw user; no authority retained."""
    if (type(inputs) is not PresenceDisplayInputs or type(profile) is not ProfilePresentation
            or type(now_ns) is not int or now_ns < 0
            or type(stale_s) not in (int, float) or not math.isfinite(stale_s)
            or not 0 < stale_s <= 86400
            or type(max_observation_s) not in (int, float)
            or not math.isfinite(max_observation_s)
            or not 0 < max_observation_s <= 300):
        raise ValueError('invalid presence presentation input')
    _text(user, 256, 'presence subject')
    wanted = [row for row in inputs.subjects if row[0] == user]
    if len(wanted) != 1:
        raise ValueError('presence subject was not captured exactly')
    stale_ns = int(stale_s * 1_000_000_000)
    observation_ns = int(max_observation_s * 1_000_000_000)
    age = now_ns - inputs.observed_ns
    if not 0 <= age < observation_ns:
        return PresencePresentation({}, True)
    _user, seen_ns, shown, online_ns = wanted[0]
    if (type(seen_ns) not in (int, float) or type(online_ns) not in (int, float)
            or not math.isfinite(seen_ns) or not math.isfinite(online_ns)
            or type(shown) is not str):
        raise ValueError('invalid captured presence')
    values = {}
    pending = False
    for field, gate in (('last_seen', 'may_see_last_seen'), ('online', 'may_see_online')):
        permitted = profile.profile.get(gate)
        if permitted is None:
            pending = True
        elif permitted is True:
            values[field] = (shown if seen_ns > 0 else '') if field == 'last_seen' \
                else bool(online_ns > 0 and online_ns >= now_ns - stale_ns)
    return PresencePresentation(values, pending)
