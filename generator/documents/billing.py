"""Invoices and credit notes computed from the true world (pure functions, no I/O).

- Platform invoices: one per ad account and month, for agency-billed clients only.
  Delivery dates are already the account's local date, which is the platform timezone.
- Credit notes: a config-driven handful, each a share of one platform invoice.
- Client invoices: one per agency-billed client and month, per billing model.
  Client-owned accounts are billed by the platform directly, so they get neither.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from generator.documents.config import DocumentsConfig
from generator.documents.model import CreditNote, Invoice, InvoiceLine
from generator.documents.numbering import (
    agency_invoice_number,
    agency_issue_date,
    credit_note_issue_date,
    credit_note_number,
    platform_invoice_number,
    platform_issue_date,
)
from generator.world import Client, World, money, month_end, rng_for, to_decimal

PLATFORM_LINE_WORDING = {
    "meta": "Facebook/Instagram ads – {ref}",
    "google": "Google Ads campaign {ref}",
    "tiktok": "TikTok For Business advertising service ({ref})",
}
CREDIT_NOTE_REASONS = (
    "Invalid traffic adjustment",
    "Billing adjustment for under-delivery",
    "Make-good for ad serving issue",
)


@dataclass(frozen=True)
class Documents:
    platform_invoices: tuple[Invoice, ...]
    credit_notes: tuple[CreditNote, ...]
    client_invoices: tuple[Invoice, ...]


def build_documents(world: World, config: DocumentsConfig) -> Documents:
    platform_invoices = build_platform_invoices(world, config)
    credit_notes = build_credit_notes(world, platform_invoices, config)
    client_invoices = build_client_invoices(world, platform_invoices, credit_notes, config)
    return Documents(platform_invoices, credit_notes, client_invoices)


def vat_and_total(subtotal: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    vat = money(subtotal * rate)
    return vat, subtotal + vat


def agency_billed(world: World) -> dict[str, Client]:
    return {c.code: c for c in world.clients if c.billing_model != "client_owned"}


# --- platform invoices --------------------------------------------------------------------


def build_platform_invoices(world: World, config: DocumentsConfig) -> tuple[Invoice, ...]:
    billed = agency_billed(world)
    campaigns = {c.campaign_ref: c for c in world.campaigns}
    # (client, platform, month) -> campaign_ref -> spend
    spend: dict[tuple[str, str, date], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for day in world.delivery:
        campaign = campaigns[day.campaign_ref]
        if campaign.client_code in billed:
            key = (campaign.client_code, campaign.platform_key, day.date.replace(day=1))
            spend[key][day.campaign_ref] += day.spend

    platforms = {p.key: p for p in world.platforms}
    accounts = {(a.client_code, a.platform_key): a for a in world.ad_accounts}
    invoices = []
    for (client_code, platform_key, month), by_ref in sorted(spend.items()):
        account = accounts[(client_code, platform_key)]
        lines = tuple(
            InvoiceLine(
                line_no=i,
                campaign_ref=ref,
                description=PLATFORM_LINE_WORDING[platform_key].format(ref=ref),
                amount=amount,
            )
            for i, (ref, amount) in enumerate(sorted(by_ref.items()), start=1)
        )
        issue = platform_issue_date(platform_key, month)
        invoices.append(
            make_invoice(
                config,
                kind="platform",
                layout=platform_key,
                vendor=platforms[platform_key].name,
                number=platform_invoice_number(
                    world.seed, platform_key, account.external_account_id, month
                ),
                client_code=client_code,
                platform_key=platform_key,
                bill_to=config.agency.name,
                account_id=account.external_account_id,
                issue_date=issue,
                month=month,
                lines=lines,
            )
        )
    return tuple(invoices)


def make_invoice(
    config: DocumentsConfig,
    *,
    kind: str,
    layout: str,
    vendor: str,
    number: str,
    client_code: str,
    platform_key: str | None,
    bill_to: str,
    account_id: str | None,
    month: date,
    issue_date: date,
    lines: tuple[InvoiceLine, ...],
) -> Invoice:
    subtotal = sum((line.amount for line in lines), Decimal(0))
    vat, total = vat_and_total(subtotal, config.vat_rate)
    return Invoice(
        kind=kind,
        layout=layout,
        vendor=vendor,
        number=number,
        client_code=client_code,
        platform_key=platform_key,
        bill_to=bill_to,
        account_id=account_id,
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=config.payment_terms_days),
        period_start=month,
        period_end=month_end(month),
        lines=lines,
        subtotal=subtotal,
        vat_rate=config.vat_rate,
        vat=vat,
        total=total,
    )


# --- credit notes -------------------------------------------------------------------------


def build_credit_notes(
    world: World, platform_invoices: tuple[Invoice, ...], config: DocumentsConfig
) -> tuple[CreditNote, ...]:
    settings = config.credit_notes
    rng = rng_for(world.seed, "credit-notes")
    candidates = sorted(platform_invoices, key=lambda i: (i.period_start, i.vendor, i.number))
    chosen = rng.sample(candidates, min(settings.count, len(candidates)))
    notes = []
    for original in sorted(chosen, key=lambda i: (i.period_start, i.vendor, i.number)):
        pct = to_decimal(round(rng.uniform(float(settings.pct_min), float(settings.pct_max)), 4))
        subtotal = money(original.subtotal * pct)
        vat, total = vat_and_total(subtotal, config.vat_rate)
        assert original.platform_key is not None
        notes.append(
            CreditNote(
                vendor=original.vendor,
                number=credit_note_number(
                    world.seed, original.platform_key, original.number, original.period_start
                ),
                original_number=original.number,
                client_code=original.client_code,
                platform_key=original.platform_key,
                bill_to=original.bill_to,
                issue_date=credit_note_issue_date(original.period_start),
                period_start=original.period_start,
                period_end=original.period_end,
                campaign_ref=original.lines[0].campaign_ref or "",
                reason=rng.choice(CREDIT_NOTE_REASONS),
                subtotal=subtotal,
                vat_rate=config.vat_rate,
                vat=vat,
                total=total,
            )
        )
    return tuple(notes)


# --- client invoices ----------------------------------------------------------------------


def build_client_invoices(
    world: World,
    platform_invoices: tuple[Invoice, ...],
    credit_notes: tuple[CreditNote, ...],
    config: DocumentsConfig,
) -> tuple[Invoice, ...]:
    billed = agency_billed(world)
    platforms = {p.key: p for p in world.platforms}
    by_client_month: dict[tuple[str, date], list[Invoice]] = defaultdict(list)
    for invoice in platform_invoices:
        by_client_month[(invoice.client_code, invoice.period_start)].append(invoice)
    credits_by_original = {note.original_number: note for note in credit_notes}

    invoices = []
    for (client_code, month), sources in sorted(by_client_month.items()):
        client = billed[client_code]
        texts: list[tuple[str | None, str, Decimal]] = []
        for source in sources:
            platform_name = platforms[source.platform_key or ""].name
            for line in source.lines:
                texts.append(
                    (
                        line.campaign_ref,
                        f"Media – {platform_name} – {line.campaign_ref}",
                        line.amount,
                    )
                )
            note = credits_by_original.get(source.number)
            if note is not None:
                texts.append(
                    (
                        note.campaign_ref,
                        f"Credit note {note.number} ({platform_name})",
                        -note.subtotal,
                    )
                )
        net_media = sum((amount for _, _, amount in texts), Decimal(0))
        texts.append(fee_line(client, net_media))
        lines = tuple(
            InvoiceLine(line_no=i, campaign_ref=ref, description=text, amount=amount)
            for i, (ref, text, amount) in enumerate(texts, start=1)
        )
        invoices.append(
            make_invoice(
                config,
                kind="client",
                layout="agency",
                vendor=config.agency.name,
                number=agency_invoice_number(config.agency.invoice_prefix, client_code, month),
                client_code=client_code,
                platform_key=None,
                bill_to=client.name,
                account_id=None,
                issue_date=agency_issue_date(month),
                month=month,
                lines=lines,
            )
        )
    return tuple(invoices)


def fee_line(client: Client, net_media: Decimal) -> tuple[None, str, Decimal]:
    if client.billing_model == "markup":
        assert client.markup_pct is not None
        return (
            None,
            f"Agency commission {client.markup_pct}% on media",
            money(net_media * client.markup_pct / 100),
        )
    if client.billing_model == "mgmt_fee":
        assert client.mgmt_fee is not None
        return None, "Management fee", client.mgmt_fee
    raise ValueError(f"client {client.code} is not billed by the agency")
