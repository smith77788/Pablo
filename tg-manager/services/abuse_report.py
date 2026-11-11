"""Легитимная жалоба по почте — ОДНА достоверная жалоба со своего ящика.

Это НЕ флот и НЕ координированная рассылка. Один оператор → один его собственный
подключённый почтовый ящик (strike_email_accounts, OAuth/SMTP его же почты) → одно
письмо в официальный abuse-адрес платформы, с доказательствами как МЕТАДАННЫМИ
(ссылки на сообщения, message_id, время обнаружения, категория, контекст).

ЖЁСТКАЯ ГРАНИЦА. Сам запрещённый материал модуль НИКОГДА не скачивает, не хранит
и не вкладывает в письмо — только ССЫЛКИ на него. Попытка передать сам контент
(data:-URI, вложенный base64-блоб) отклоняется валидатором.

ЧЕСТНЫЕ СТАТУСЫ. draft / validated / sent / send_failed / no_mailbox. «Отправлено»
≠ «платформа удалила ресурс»: решение принимает модерация, модуль этого не знает и
за факт не выдаёт. Для CSAM отдельно подсказываем эскалацию в NCMEC/IWF — это
органы, которые реально запускают удаление и расследование.
"""
from __future__ import annotations

import logging
import re
import secrets as _secrets
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# ── Категории нарушений (ЕДИНЫЙ закрытый список для обоих векторов) ───────────
# Один источник правды для модуля Жалоб: категория сразу задаёт и email-письмо
# (subject/escalate/получатель), и повод in-app жалобы флотом.
#   tg_reason — ключ reporter_engine.REPORT_REASONS: повод MTProto-жалобы, который
#               подадут выбранные аккаунты флота по этой же категории.
#   escalate  — внеплатформенные органы (веб-формы), которые реально действуют;
#               их оператор подаёт сам (мы не вкладываем сам материал).
CATEGORIES: dict[str, dict] = {
    "csam": {
        "label": "Насилие над детьми (CSAM)",
        "subject": "Report: child sexual abuse material (CSAM)",
        "tg_reason": "childabuse",
        "escalate": ["NCMEC CyberTipline — report.cybertip.org",
                     "IWF — report.iwf.org.uk"],
    },
    "terrorism": {
        "label": "Терроризм / призывы к насилию",
        "subject": "Report: terrorism / incitement to violence",
        "tg_reason": "violence",
        "escalate": [],
    },
    "scam": {
        "label": "Мошенничество / обман",
        "subject": "Report: fraud / scam",
        "tg_reason": "fake",
        "escalate": [],
    },
    "drugs": {
        "label": "Продажа запрещённых веществ",
        "subject": "Report: sale of illegal substances",
        "tg_reason": "drugs",
        "escalate": [],
    },
    "porn": {
        "label": "Порнография (18+)",
        "subject": "Report: pornography",
        "tg_reason": "porn",
        "escalate": [],
    },
    "copyright": {
        "label": "Нарушение авторских прав (правообладатель)",
        "subject": "Copyright infringement report",
        "tg_reason": "other",
        "escalate": [],
    },
    "privacy": {
        "label": "Нарушение приватности / персональные данные",
        "subject": "Privacy violation report",
        "tg_reason": "personal",
        "escalate": [],
    },
    "intimate": {
        "label": "Интимные материалы без согласия",
        "subject": "Non-consensual intimate imagery report",
        "tg_reason": "porn",
        "escalate": [],
    },
    "spam": {
        "label": "Спам / массовая рассылка",
        "subject": "Report: spam / unsolicited bulk messaging",
        "tg_reason": "spam",
        "escalate": [],
    },
    "other": {
        "label": "Другое нарушение правил Telegram",
        "subject": "Report: Telegram Terms of Service violation",
        "tg_reason": "other",
        "escalate": [],
    },
}

# Повод MTProto-жалобы по умолчанию, если у категории нет своего (или она чужая).
_DEFAULT_TG_REASON = "other"


def tg_reason_for(category: str) -> str:
    """Повод in-app жалобы флотом по категории (ключ reporter_engine.REPORT_REASONS)."""
    return str(CATEGORIES.get(category, {}).get("tg_reason") or _DEFAULT_TG_REASON)

_MAX_EVIDENCE = 30
_MAX_CONTEXT = 4000
_MIN_CONTEXT = 20

# Признак ССЫЛКИ/идентификатора, а не самого материала.
_LINK_RE = re.compile(r"(https?://|t\.me/|@[\w\d_]{3,}|#?\d{5,})", re.I)
# Признак попытки передать САМ контент — отклоняем.
_MATERIAL_RE = re.compile(r"(data:|base64,|;base64|\.(jpg|jpeg|png|mp4|webm|gif)\b)", re.I)


def categories_public() -> list[dict]:
    """Список категорий для UI: [{key, label, tg_reason}].

    tg_reason отдаётся, чтобы единый модуль Жалоб одной категорией подал и
    письмо (email-вектор), и in-app жалобу флотом (reporter_engine).
    """
    return [{"key": k, "label": v["label"],
             "tg_reason": v.get("tg_reason", _DEFAULT_TG_REASON)}
            for k, v in CATEGORIES.items()]


def recipients_for(category: str) -> list[str]:
    from services.report_channels import email_recipient
    return [email_recipient(category)]


def escalation_for(category: str) -> list[str]:
    """Внеплатформенные органы (веб-формы), куда жалобу подаёт сам оператор."""
    return list(CATEGORIES.get(category, {}).get("escalate", []))


def _is_evidence_ref(s: str) -> bool:
    """Строка — это ССЫЛКА/ID на нарушение, а не вложенный материал."""
    s = (s or "").strip()
    if not s or len(s) > 500:
        return False
    if _MATERIAL_RE.search(s):
        return False  # попытка передать сам контент — не метаданные
    return bool(_LINK_RE.search(s))


def _norm_target(raw: str) -> str:
    """Нормализовать цель (@username / t.me / id). Переиспуем парсер репортера."""
    try:
        from services.reporter_engine import parse_target_ref
        t = parse_target_ref(raw)
        if t:
            return t
    except Exception:
        pass
    return (raw or "").strip()


def validate_report(data: Any) -> tuple[Optional[dict], list[str]]:
    """Проверить обращение. Возвращает (чистое|None, ошибки по-русски).

    Доказательства — только ссылки/ID (метаданные). Сам материал отклоняется.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["Ожидался объект с полями обращения"]

    target = _norm_target(str(data.get("target") or ""))
    if not target:
        errors.append("Укажите цель: @username, ссылку t.me/… или ID")

    category = str(data.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        errors.append("Категория нарушения не из списка")

    raw_ev = data.get("evidence") or []
    if isinstance(raw_ev, str):
        raw_ev = [x for x in re.split(r"[\n,; ]+", raw_ev) if x.strip()]
    if not isinstance(raw_ev, list):
        raw_ev = []
    evidence: list[str] = []
    rejected_material = False
    for item in raw_ev[:_MAX_EVIDENCE]:
        s = str(item or "").strip()
        if not s:
            continue
        if _MATERIAL_RE.search(s):
            rejected_material = True
            continue
        if _is_evidence_ref(s):
            evidence.append(s)
    if rejected_material:
        errors.append("Нельзя прикладывать сам материал — только ССЫЛКИ и ID "
                      "(ссылка на сообщение, message_id). Сам контент передавать запрещено.")
    if not evidence:
        errors.append("Нужна хотя бы одна проверяемая ССЫЛКА или ID на нарушение")

    context = " ".join(str(data.get("context") or "").split())
    if len(context) < _MIN_CONTEXT:
        errors.append(f"Опишите нарушение словами (не меньше {_MIN_CONTEXT} символов)")
    context = context[:_MAX_CONTEXT]
    from services.report_channels import email_recipient
    recipient_key = str(data.get('recipient_key') or '').strip() or None
    try:
        email_recipient(category, recipient_key)
    except ValueError as exc:
        errors.append(str(exc))

    if errors:
        return None, errors
    return {
        "target": target,
        "category": category,
        "evidence": evidence,
        "context": context,
        "recipient_key": recipient_key,
    }, []


def compose_email(report: dict, *, ref_id: str, now: Optional[datetime] = None) -> tuple[str, str]:
    """Собрать письмо-обращение (subject, body). Только факты и ссылки, без материала."""
    now = now or datetime.now(timezone.utc)
    cat = CATEGORIES.get(report["category"], CATEGORIES["other"])
    subject = f"{cat['subject']} — {report['target']} [ref {ref_id}]"
    lines = [
        "Dear Telegram Moderation,",
        "",
        "We report the following resource for a suspected Terms of Service / "
        "legal violation and ask you to review it. The decision to restrict or "
        "remove the resource is yours; this is a report with evidence, not a demand.",
        "",
        f"Target: {report['target']}",
        f"Category: {cat['subject']}",
        f"Detected (UTC): {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        "Evidence (links / message IDs — the material itself is NOT attached):",
    ]
    for i, ref in enumerate(report["evidence"], 1):
        lines.append(f"  {i}. {ref}")
    lines += [
        "",
        "Context / description of the violation:",
        report["context"],
        "",
        f"Reference: {ref_id}",
        "",
        "Submitted by the resource operator via their own mailbox. "
        "We have not downloaded, stored or attached any of the reported material.",
    ]
    if report["category"] == "csam":
        lines += [
            "",
            "This report concerns suspected child sexual abuse material. It is also "
            "being escalated to NCMEC (report.cybertip.org) and IWF (report.iwf.org.uk). "
            "Telegram has obligations under 18 U.S.C. § 2258A to report CSAM to NCMEC.",
        ]
    return subject, "\n".join(lines)


# ── Отправка (ОДНО письмо с ОДНОГО ящика оператора) ──────────────────────────

SendFn = Callable[[dict, list[str], str, str], Awaitable[tuple[bool, str]]]


async def _primary_mailbox(pool, owner_id: int, mailbox_id: int | None = None) -> Optional[dict]:
    """Первый активный подключённый ящик оператора (наименее недавно использованный)."""
    try:
        row = await pool.fetchrow(
            "SELECT id, email, smtp_host, smtp_port, smtp_pass, "
            "COALESCE(auth_type,'password') AS auth_type, oauth_provider, "
            "oauth_refresh_token, oauth_access_token, oauth_expires_at "
            "FROM strike_email_accounts WHERE owner_id=$1 AND is_active=TRUE "
            + ("AND id=$2 " if mailbox_id is not None else "")
            + "ORDER BY last_used_at ASC NULLS FIRST LIMIT 1",
            *([int(owner_id), mailbox_id] if mailbox_id is not None else [int(owner_id)]))
    except Exception:
        log.debug("abuse_report._primary_mailbox failed owner=%s", owner_id, exc_info=True)
        return None
    return dict(row) if row else None


async def _default_send(pool, mailbox: dict, recipients: list[str], subject: str,
                        body: str) -> tuple[bool, str]:
    """Отправить ОДНО письмо со своего ящика. Переиспуем нейтральный SMTP-примитив.

    Нейтральные части (расшифровка секрета ящика, SMTP+XOAUTH2) берём из уже
    существующего примитива — своего кода для отправки не плодим.
    """
    try:
        from services.strike_engine import _email_secret_for_account, _send_email
        from services.token_vault import decrypt_token
        mailbox = dict(mailbox)
        if (mailbox.get('auth_type') or 'password') != 'oauth':
            secret = decrypt_token(mailbox.get('smtp_pass') or '')
            if not secret or secret.startswith('ENC:'):
                return False, 'Не удалось прочитать пароль ящика. Переподключите почту.'
            mailbox['smtp_pass'] = secret
        secret, auth_type = await _email_secret_for_account(pool, mailbox)
        ok, err = await _send_email(
            mailbox["smtp_host"], mailbox["smtp_port"], mailbox["email"], secret,
            mailbox["email"], ", ".join(recipients), subject, body, auth_type=auth_type)
        return bool(ok), str(err or "")
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]


def _ref_id() -> str:
    return "AR-" + _secrets.token_hex(4).upper()


async def send_report(pool, owner_id: int, data: Any, *,
                      send_fn: Optional[SendFn] = None,
                      now: Optional[datetime] = None) -> dict:
    """Проверить и отправить ОДНУ жалобу со своего ящика. Честный статус.

    Статусы: invalid | no_mailbox | sent | send_failed. «sent» — письмо ушло, это
    НЕ «ресурс заблокирован». Любой исход пишется в журнал abuse_reports.
    """
    now = now or datetime.now(timezone.utc)
    clean, errors = validate_report(data)
    if errors:
        return {"status": "invalid", "errors": errors}

    ref_id = _ref_id()
    raw_mailbox_id = data.get('mailbox_id')
    try:
        mailbox_id = int(raw_mailbox_id) if raw_mailbox_id is not None else None
        if mailbox_id is not None and not 0 < mailbox_id < 2**63:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return {'status': 'invalid', 'errors': ['Неверный почтовый ящик']}
    mailbox = await _primary_mailbox(pool, owner_id, mailbox_id)
    from services.report_channels import email_recipient
    recipients = [email_recipient(clean['category'], clean.get('recipient_key'))]
    escalation = escalation_for(clean["category"])

    if not mailbox:
        await _store(pool, owner_id, clean, ref_id, status="no_mailbox",
                     recipient=", ".join(recipients), sent_at=None)
        return {"status": "no_mailbox", "ref_id": ref_id, "recipients": recipients,
                "escalation": escalation,
                "message": "Почтовый ящик не подключён. Подключите свою почту "
                           "(раздел почты), и жалоба уйдёт с неё."}

    subject, body = compose_email(clean, ref_id=ref_id, now=now)
    if send_fn is None:
        async def send_fn(mb, rcpts, subj, bd):  # noqa: E731 — связываем pool
            return await _default_send(pool, mb, rcpts, subj, bd)
    try:
        ok, err = await send_fn(mailbox, recipients, subject, body)
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)[:160]
    status = "sent" if ok else "send_failed"
    await _store(pool, owner_id, clean, ref_id, status=status,
                 recipient=", ".join(recipients), sent_at=(now if ok else None),
                 error=(None if ok else err))
    return {"status": status, "ref_id": ref_id, "recipients": recipients,
            "escalation": escalation, "error": (None if ok else err),
            "note": "Письмо отправлено. Решение о блокировке принимает модерация "
                    "платформы — это НЕ подтверждение удаления ресурса."
                    if ok else "Не удалось отправить письмо с вашего ящика."}


async def send_report_multi(pool, owner_id: int, data: Any, mailbox_ids: list[int], *,
                            send_fn: Optional[SendFn] = None,
                            now: Optional[datetime] = None) -> dict:
    """Отправить жалобу с НЕСКОЛЬКИХ ящиков оператора (по одному письму с каждого).

    Единый модуль Жалоб: оператор выбирает один или несколько своих подключённых
    ящиков, и с каждого уходит отдельное письмо в компетентный орган с
    автоподстановкой текста по категории. Исход по ФАКТУ, по каждому ящику
    отдельно (sent/send_failed/no_mailbox/invalid) — без ложного успеха.

    Валидация обращения — ОДИН раз: если ссылки/контекст неполны, не шлём ни
    одного письма и возвращаем ошибку (иначе каждый ящик вернул бы тот же список).
    """
    ids: list[int] = []
    for raw in (mailbox_ids or []):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    # Дедуп, сохраняя порядок.
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {"status": "invalid", "errors": ["Не выбран ни один почтовый ящик"],
                "requested": 0, "sent": 0, "failed": 0, "no_mailbox": 0, "results": []}

    clean, errors = validate_report(data)
    if errors:
        return {"status": "invalid", "errors": errors, "requested": len(ids),
                "sent": 0, "failed": 0, "no_mailbox": 0, "results": []}

    results: list[dict] = []
    sent = failed = no_mailbox = 0
    recipients: list[str] = []
    escalation: list[str] = escalation_for(clean["category"])
    for mid in ids:
        res = await send_report(pool, owner_id, {**clean, "mailbox_id": mid},
                                send_fn=send_fn, now=now)
        status = res.get("status")
        recipients = res.get("recipients") or recipients
        if status == "sent":
            sent += 1
        elif status == "no_mailbox":
            no_mailbox += 1
        else:
            failed += 1
        results.append({"mailbox_id": mid, "status": status,
                        "ref_id": res.get("ref_id"), "error": res.get("error")})
    return {"status": "sent" if sent else ("no_mailbox" if no_mailbox and not failed
                                           else "send_failed"),
            "requested": len(ids), "sent": sent, "failed": failed,
            "no_mailbox": no_mailbox, "results": results,
            "recipients": recipients, "escalation": escalation}


async def _store(pool, owner_id: int, clean: dict, ref_id: str, *, status: str,
                 recipient: str, sent_at, error: Optional[str] = None) -> None:
    """Журнал обращения (метаданные, не материал). Fail-soft."""
    import json
    try:
        await pool.execute(
            "INSERT INTO abuse_reports(owner_id, ref_id, target, category, evidence, "
            "context, recipient, status, error, sent_at) "
            "VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10)",
            int(owner_id), ref_id, clean["target"], clean["category"],
            json.dumps(clean["evidence"], ensure_ascii=False), clean["context"],
            recipient, status, (error or None), sent_at)
    except Exception:
        log.debug("abuse_report._store failed owner=%s ref=%s", owner_id, ref_id, exc_info=True)


async def history(pool, owner_id: int, limit: int = 30) -> list[dict]:
    """Последние обращения оператора (журнал, метаданные)."""
    try:
        rows = await pool.fetch(
            "SELECT ref_id, target, category, status, recipient, created_at, sent_at "
            "FROM abuse_reports WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
            int(owner_id), max(1, min(int(limit), 100)))
    except Exception:
        return []
    return [{"ref_id": r["ref_id"], "target": r["target"], "category": r["category"],
             "status": r["status"], "recipient": r["recipient"],
             "created_at": r["created_at"].isoformat() if r["created_at"] else None,
             "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None}
            for r in rows or []]
