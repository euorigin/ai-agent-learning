"""内容服务：角色卡与世界知识的加载、校验、版本化与运营开关。

文档 §3.1：角色卡由策划维护，作为唯一事实源，需"配置变更应有版本号、审批记录和回滚能力"。
文档 §3.9 内容层：角色卡进入审核流程（approved 标记）。
文档 §3.9 运营层：支持禁用角色、回滚配置。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

import yaml

from ..schemas import CharacterCard

CARD_DIR = Path(__file__).resolve().parent / "cards"
LORE_FILE = Path(__file__).resolve().parent / "lore.yaml"
WORLD_FILE = Path(__file__).resolve().parent / "world.yaml"

REQUIRED_FIELDS = ("id", "name", "identity", "role_type")


class CardValidationError(ValueError):
    pass


class ContentService:
    """内存态内容服务：加载 → 校验 → 版本化 → 发布/回滚/禁用。"""

    def __init__(
        self,
        card_dir: Path = CARD_DIR,
        lore_file: Path = LORE_FILE,
        world_file: Path = WORLD_FILE,
    ) -> None:
        self.card_dir = card_dir
        self.lore_file = lore_file
        self.world_file = world_file
        self.cards: dict[str, CharacterCard] = {}
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.disabled: set[str] = set()
        self.lore: list[dict[str, Any]] = []
        self.world: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if self.card_dir.is_dir():
            for path in sorted(self.card_dir.glob("*.yaml")):
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                card = self._validate(data, origin=path.name)
                self.cards[card.id] = card
                self.versions[card.id] = [card.model_dump(mode="json")]
        if self.lore_file.exists():
            self.lore = yaml.safe_load(self.lore_file.read_text(encoding="utf-8")) or []
        if self.world_file.exists():
            self.world = yaml.safe_load(self.world_file.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _validate(data: dict[str, Any], origin: str = "") -> CharacterCard:
        missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
        if missing:
            raise CardValidationError(f"角色卡 {origin} 缺少必填字段: {missing}")
        card = CharacterCard(**data)
        if not card.approved:
            raise CardValidationError(f"角色卡 {card.id} 未通过内容审核（approved=false）")
        return card

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(self, card_id: str) -> Optional[CharacterCard]:
        return self.cards.get(card_id)

    def require(self, card_id: str) -> CharacterCard:
        card = self.cards.get(card_id)
        if card is None:
            raise KeyError(f"未知角色卡: {card_id}")
        return card

    def is_enabled(self, card_id: str) -> bool:
        return card_id in self.cards and card_id not in self.disabled

    def list_cards(self) -> list[CharacterCard]:
        return list(self.cards.values())

    def list_public(self) -> list[dict[str, Any]]:
        """给前端的角色卡摘要（不含内部提示词细节）。"""
        out = []
        for c in self.cards.values():
            out.append({
                "id": c.id,
                "name": c.name,
                "role_type": c.role_type,
                "version": c.version,
                "identity": c.identity,
                "personality": c.personality,
                "values": c.values,
                "goals": c.goals.model_dump(),
                "knowledge_scope": c.knowledge_scope,
                "taboos": c.taboos,
                "tool_permissions": c.tool_permissions,
                "greeting": c.greeting,
                "voice": c.voice.model_dump(),
                "enabled": self.is_enabled(c.id),
            })
        return out

    def knowledge_for(self, card: CharacterCard) -> list[dict[str, Any]]:
        """按角色知识范围过滤世界知识（文档 §3.3：区分可知 / 未知）。"""
        scope = set(card.knowledge_scope)
        out = []
        for entry in self.lore:
            topics = set(entry.get("topics", []))
            if not scope or topics & scope:
                out.append(entry)
        return out

    def unknown_topics(self, card: CharacterCard) -> list[str]:
        """角色明确不该知道的话题——用于边界提示与拒答。"""
        known = set(card.knowledge_scope)
        unknown = []
        for entry in self.lore:
            topics = set(entry.get("topics", []))
            if topics and not (topics & known):
                unknown.extend(t for t in topics if t not in known)
        return sorted(set(unknown))

    # ------------------------------------------------------------------ #
    # 运营写操作（文档 §5.2 CRUD / 回滚）
    # ------------------------------------------------------------------ #
    def publish(self, data: dict[str, Any], approver: str = "ops") -> CharacterCard:
        card = self._validate(data, origin="publish")
        old = self.cards.get(card.id)
        if old is not None and old.version == card.version and old.model_dump() == card.model_dump():
            return old
        card.changelog.append({
            "version": card.version,
            "approver": approver,
            "from": old.version if old else None,
        })
        self.cards[card.id] = card
        self.versions.setdefault(card.id, []).append(card.model_dump(mode="json"))
        return card

    def rollback(self, card_id: str, version: Optional[str] = None) -> CharacterCard:
        history = self.versions.get(card_id, [])
        if not history:
            raise KeyError(f"角色卡 {card_id} 无历史版本")
        if version is None:
            if len(history) < 2:
                raise ValueError(f"角色卡 {card_id} 只有一个版本，无法回滚")
            target = history[-2]
            history.pop()
        else:
            target = next((h for h in history if h.get("version") == version), None)
            if target is None:
                raise KeyError(f"角色卡 {card_id} 不存在版本 {version}")
        card = CharacterCard(**target)
        self.cards[card_id] = card
        return card

    def disable(self, card_id: str, disabled: bool = True) -> bool:
        if card_id not in self.cards:
            raise KeyError(card_id)
        if disabled:
            self.disabled.add(card_id)
        else:
            self.disabled.discard(card_id)
        return self.is_enabled(card_id)

    def export(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(c.model_dump(mode="json")) for c in self.cards.values()]


content_service = ContentService()
