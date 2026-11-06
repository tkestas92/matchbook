"""Settings for rendered documents: the `agency` and `documents` blocks of config.yaml."""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from generator.world import DEFAULT_CONFIG_PATH, ConfigError, to_decimal


@dataclass(frozen=True)
class AgencyConfig:
    name: str
    vat_code: str
    invoice_prefix: str


@dataclass(frozen=True)
class CreditNoteConfig:
    count: int
    pct_min: Decimal
    pct_max: Decimal


@dataclass(frozen=True)
class DocumentsConfig:
    agency: AgencyConfig
    vat_rate: Decimal
    payment_terms_days: int
    credit_notes: CreditNoteConfig


def load_documents_config(path: Path = DEFAULT_CONFIG_PATH) -> DocumentsConfig:
    return parse_documents_config(yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_documents_config(raw: dict[str, Any]) -> DocumentsConfig:
    agency, docs = raw["agency"], raw["documents"]
    credit = docs["credit_notes"]
    config = DocumentsConfig(
        agency=AgencyConfig(
            name=str(agency["name"]),
            vat_code=str(agency["vat_code"]),
            invoice_prefix=str(agency["invoice_prefix"]),
        ),
        vat_rate=to_decimal(docs["vat_rate"]),
        payment_terms_days=int(docs["payment_terms_days"]),
        credit_notes=CreditNoteConfig(
            count=int(credit["count"]),
            pct_min=to_decimal(credit["pct"]["min"]),
            pct_max=to_decimal(credit["pct"]["max"]),
        ),
    )
    validate_documents_config(config)
    return config


def validate_documents_config(config: DocumentsConfig) -> None:
    if not 0 <= config.vat_rate < 1:
        raise ConfigError("documents.vat_rate must be between 0 and 1")
    if config.payment_terms_days < 0:
        raise ConfigError("documents.payment_terms_days must not be negative")
    credit = config.credit_notes
    if credit.count < 0:
        raise ConfigError("documents.credit_notes.count must not be negative")
    if not 0 < credit.pct_min <= credit.pct_max < 1:
        raise ConfigError("documents.credit_notes.pct must satisfy 0 < min <= max < 1")
    if not config.agency.invoice_prefix.isalnum():
        raise ConfigError("agency.invoice_prefix must be alphanumeric")
