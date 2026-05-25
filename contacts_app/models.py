from __future__ import annotations

from dataclasses import dataclass, field


KEEP_SCORE_HALF_LIFE_DAYS = 730
GROUP_MESSAGE_WEIGHT = 0.25


@dataclass
class Contact:
    source_id: str
    source_uuid: str
    source_path: str
    account_name: str
    account_key: str
    record_id: int
    contact_identifier: str
    name: str
    first_name: str = ""
    last_name: str = ""
    organization: str = ""
    nickname: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.source_id}:{self.record_id}"

    @property
    def handles(self) -> list[str]:
        return list(dict.fromkeys([*self.phones, *self.emails]))


@dataclass
class ContactMetrics:
    sent_total: int = 0
    sent_direct: int = 0
    sent_group: int = 0
    received_from_them_total: int = 0
    shared_total: int = 0
    direct_chat_count: int = 0
    group_chat_count: int = 0
    last_sent_ns: int | None = None
    last_message_ns: int | None = None
    matched_handles: set[str] = field(default_factory=set)


