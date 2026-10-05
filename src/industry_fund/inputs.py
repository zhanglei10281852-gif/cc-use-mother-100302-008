"""命令输入解析：把外部字典严格校验为领域对象。"""

from __future__ import annotations

from typing import Any

from .contracts import Money
from .domain import ApplicationContent, ApplicationKind, RelatedParty
from .errors import ValidationError


def parse_money(value: Any, label: str = "金额") -> Money:
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        if not text:
            raise ValidationError(f"{label}不能为空")
        try:
            return Money.yuan(text)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{label}格式非法: {text}") from exc
    if isinstance(value, dict):
        cents = value.get("cents")
        currency = value.get("currency", "CNY")
        if not isinstance(cents, int) or cents < 0:
            raise ValidationError(f"{label}的 cents 必须为非负整数")
        return Money(cents, currency)
    raise ValidationError(f"{label}格式非法")


def parse_related_party(item: dict[str, Any]) -> RelatedParty:
    try:
        return RelatedParty(
            name=str(item["name"]).strip(),
            relation=str(item["relation"]).strip(),
            party_type=str(item.get("party_type", "org")).strip(),
            detail=str(item.get("detail", "")).strip(),
        )
    except KeyError as exc:
        raise ValidationError(f"关联方缺少字段: {exc.args[0]}") from exc


def parse_content(data: dict[str, Any]) -> ApplicationContent:
    required = ("project_name", "kind", "tech_route", "team", "fund_usage", "related_parties")
    for key in required:
        if key not in data:
            raise ValidationError(f"申请材料缺少字段: {key}")
    kind = data["kind"]
    valid_kinds = {k.value for k in ApplicationKind}
    if kind not in valid_kinds:
        raise ValidationError(f"申请类别非法: {kind}（可选 {sorted(valid_kinds)}）")
    if not str(data["project_name"]).strip() or not str(data["tech_route"]).strip():
        raise ValidationError("项目名称与技术路线不能为空")
    team = data["team"]
    if not isinstance(team, list) or not team:
        raise ValidationError("团队信息不能为空")
    normalized_team: list[dict[str, Any]] = []
    for member in team:
        if not isinstance(member, dict) or not str(member.get("name", "")).strip():
            raise ValidationError("团队成员必须包含姓名")
        normalized_team.append(dict(member))
    fund_usage = data["fund_usage"]
    if not isinstance(fund_usage, list) or not fund_usage:
        raise ValidationError("资金用途不能为空")
    normalized_usage: list[dict[str, Any]] = []
    usage_total = 0
    for item in fund_usage:
        if not isinstance(item, dict) or not str(item.get("purpose", "")).strip():
            raise ValidationError("每项资金用途必须包含 purpose")
        amount = parse_money(item.get("amount"), "资金用途金额")
        usage_total += amount.cents
        normalized = dict(item)
        normalized["amount"] = amount.to_dict()
        normalized_usage.append(normalized)
    requested = parse_money(data.get("requested_amount"), "申请金额")
    if requested.cents <= 0:
        raise ValidationError("申请金额必须大于零")
    if usage_total != requested.cents:
        raise ValidationError(
            f"资金用途合计必须等于申请金额: {usage_total} != {requested.cents}（分）")
    parties = [parse_related_party(p) for p in data.get("related_parties", [])]
    return ApplicationContent(
        project_name=str(data["project_name"]).strip(),
        kind=kind,
        tech_route=str(data["tech_route"]).strip(),
        team=normalized_team,
        fund_usage=normalized_usage,
        related_parties=parties,
        requested_amount=requested,
    )
