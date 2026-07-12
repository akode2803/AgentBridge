"""Mesh services: messaging, membership, overlays, read model, sync — glued
by the Mesh facade."""

from . import authz, events
from .accounts import AccountsService
from .directory import Directory
from .membership import MembershipService
from .messaging import MessagingService
from .overlays import ChatOverlays, UserState
from .paths import P
from .presence import PresenceService
from .privacy import PrivacyService
from .receipts import ReceiptsService
from .readmodel import build_messages, parse_tags, unread_info
from .sealer import PlainSealer, Sealer
from .service import Mesh
from .sync import SyncEngine

__all__ = [
    "Mesh", "MessagingService", "MembershipService", "PrivacyService",
    "AccountsService", "PresenceService", "ReceiptsService", "Directory",
    "SyncEngine", "ChatOverlays", "UserState", "P", "Sealer", "PlainSealer",
    "authz", "events", "build_messages", "parse_tags", "unread_info",
]
