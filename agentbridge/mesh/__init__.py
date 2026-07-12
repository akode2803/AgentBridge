"""Mesh services: messaging, membership, overlays, read model, sync — glued
by the Mesh facade."""

from . import authz, events, eventbus
from .accounts import AccountsService
from .directory import Directory
from .eventbus import Event, EventBus
from .keyring import ChatKeyService, KeyStore
from .membership import MembershipService
from .messaging import MessagingService
from .notify import CommandHook, Notification, Notifier
from .overlays import ChatOverlays, UserState
from .paths import P
from .presence import PresenceService
from .privacy import PrivacyService
from .receipts import ReceiptsService
from .readmodel import build_messages, parse_tags, unread_info
from .sealer import E2EESealer, PlainSealer, Sealer
from .service import Mesh
from .sync import SyncEngine

__all__ = [
    "Mesh", "MessagingService", "MembershipService", "PrivacyService",
    "AccountsService", "PresenceService", "ReceiptsService", "Directory",
    "KeyStore", "ChatKeyService", "SyncEngine", "ChatOverlays", "UserState",
    "P", "Sealer", "PlainSealer", "E2EESealer", "EventBus", "Event",
    "Notifier", "Notification", "CommandHook", "authz", "events", "eventbus",
    "build_messages", "parse_tags", "unread_info",
]
