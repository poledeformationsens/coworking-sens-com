"""
Module Devis L'Atelier du Coworking
====================================
Workflow :
  draft → sent → validated → acompte_paid → fully_paid

Endpoints exposés (via router) :
  POST  /api/coworking/devis                       → créer un devis (admin)
  POST  /api/coworking/devis/{id}/send             → envoyer le devis au client
  POST  /api/coworking/devis/{id}/validate         → valider (génère facture acompte + email)
  POST  /api/coworking/devis/{id}/mark-acompte     → acompte reçu (PIN + email confirmation)
  POST  /api/coworking/devis/{id}/mark-solde       → solde reçu (facture solde + email)
  POST  /api/coworking/devis/{id}/mark-total       → total reçu en une fois (saut acompte)
  POST  /api/coworking/devis/{id}/cancel           → annuler le devis
  GET   /api/coworking/devis/{id}.pdf              → PDF du devis
  GET   /api/coworking/devis                       → liste pour dashboard admin
  GET   /api/coworking/admin/dashboard             → sert le dashboard HTML

À monter dans pole_sens.py :
    from coworking_devis import router as devis_router
    app.include_router(devis_router)

Env vars requises :
    COWORKING_ADMIN_TOKEN : Bearer token pour endpoints admin coworking (génère via `openssl rand -hex 32`)
"""

import io
import os
import hashlib
import secrets
from datetime import datetime, timedelta, date, timezone
from typing import Optional, Any, List

import httpx
from fastapi import APIRouter, HTTPException, Response, Header, Body, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from reportlab.lib import colors as rlcolors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

# Réutilise les helpers / constantes du module facture existant
from webhook_coworking import (
    ACW_NAVY, ACW_GOLD, ACW_CREAM, ACW_SLATE, ACW_LIGHT_GREY, ACW_GREEN,
    _get_logo_bytes, _format_money, _format_siret, _compute_datetimes, _pin_window,
    _format_french_date, _first_name,
    _send_coworking_email, _build_confirmation_email_html, _generate_reference,
    _build_admin_notif_html, COWORKING_NOTIF_EMAIL,
    COWORKING_DISPLAY_NAME, COWORKING_LEGAL_NAME,
    COWORKING_ADDRESS_LINE1, COWORKING_ADDRESS_LINE2,
    COWORKING_PHONE, COWORKING_EMAIL,
    COWORKING_SIRET, COWORKING_VAT_NUMBER, COWORKING_WEBSITE,
    COWORKING_BANK_NAME, COWORKING_IBAN, COWORKING_BIC,
    COWORKING_APP_BASE_URL,
    IGLOOHOME_DEVICE_ID_COWORKING,
    generate_coworking_avoir_pdf,
    STRIPE_SECRET_KEY_COWORKING, STRIPE_SECRET_KEY_COWORKING_TEST,
)

router = APIRouter(prefix="/api/coworking", tags=["coworking-devis"])

# Couleurs spécifiques
ACW_CORAL = rlcolors.HexColor("#EA584A")

# Valeurs par défaut
DEFAULT_ACOMPTE_RATIO = 0.30
DEFAULT_DEVIS_VALIDITY_DAYS = 7

# Auth admin — spécifique au coworking (le pôle iad/Viseeon aura son propre token)
COWORKING_ADMIN_TOKEN = os.getenv("COWORKING_ADMIN_TOKEN", "")
COWORKING_ADMIN_EMAIL = os.getenv("COWORKING_ADMIN_EMAIL", "")
COWORKING_ADMIN_PASSWORD = os.getenv("COWORKING_ADMIN_PASSWORD", "")
COWORKING_ADMIN_RECOVERY_EMAIL = os.getenv("COWORKING_ADMIN_RECOVERY_EMAIL", "david.landry@iadfrance.fr")
# Clé dédiée à l'ingestion de prospects par la tâche de prospection quotidienne (droit limité)
COWORKING_PROSPECT_INGEST_KEY = os.getenv("PROSPECT_INGEST_KEY", "")


# ============================================================================
# Auth helper
# ============================================================================
def _check_admin(authorization: Optional[str]) -> None:
    if not COWORKING_ADMIN_TOKEN:
        raise HTTPException(500, "COWORKING_ADMIN_TOKEN non configuré côté serveur")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token manquant")
    token = authorization.split(" ", 1)[1].strip()
    if token != COWORKING_ADMIN_TOKEN:
        raise HTTPException(403, "Token admin invalide")


# ============================================================================
# Pydantic models
# ============================================================================
class LineItem(BaseModel):
    description: str
    quantity: float = 1
    unit_price_ht: float
    tva_rate: float = 20.0


class DevisCreateRequest(BaseModel):
    client_name: str
    client_email: str  # validation côté client HTML
    client_phone: Optional[str] = None
    client_type: str = Field("perso", pattern="^(perso|pro)$")
    company: Optional[str] = None
    company_siret: Optional[str] = None
    company_address: Optional[str] = None

    date: date
    hour_from: str
    hour_to: str
    space: str
    space_unit: Optional[str] = None  # ex: "Bureau 1", "Poste 3"
    slot: str = "hour"  # hour | morning | afternoon | day

    items: list[LineItem]

    acompte_ratio: float = DEFAULT_ACOMPTE_RATIO
    validity_days: int = DEFAULT_DEVIS_VALIDITY_DAYS
    admin_notes: Optional[str] = None

    # Mode TEST : si True, emails redirigés vers admin, créneau pas bloqué, etc.
    test_mode: bool = False
    # Force la création : bypass capacité + privatisation exclusive (admin override exceptionnel)
    force_create: bool = False


class DevisResponse(BaseModel):
    id: str
    devis_reference: str
    status: str
    amount_total_ttc: float
    amount_acompte_ttc: float


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    email: str


# ============================================================================
# Supabase helpers (utilise le client de pole_sens)
# ============================================================================
def _supabase():
    """Récupère le client Supabase (lazy import pour éviter import circulaire)."""
    from pole_sens import supabase  # type: ignore
    return supabase


def _fetch_devis(devis_id: str) -> Optional[dict]:
    sb = _supabase()
    res = sb.table("cw_reservations").select("*").eq("id", devis_id).limit(1).execute()
    if res.data:
        return res.data[0]
    return None


def _update_devis(devis_id: str, patch: dict) -> dict:
    sb = _supabase()
    res = sb.table("cw_reservations").update(patch).eq("id", devis_id).execute()
    return res.data[0] if res.data else {}


def _generate_next_devis_reference() -> str:
    """Appelle la fonction Postgres next_devis_reference()."""
    sb = _supabase()
    res = sb.rpc("next_devis_reference").execute()
    if isinstance(res.data, str):
        return res.data
    if isinstance(res.data, list) and res.data:
        return res.data[0]
    raise RuntimeError(f"Échec génération référence devis : {res.data}")


def _block_slot(devis: dict) -> None:
    # En mode TEST, ne bloque pas le créneau (pas de pollution du planning réel)
    if devis.get("test_mode"):
        return
    sb = _supabase()
    sb.table("cw_blocked_slots").insert({
        "date": devis["date"],
        "hour_from": devis.get("hour_from"),
        "hour_to": devis.get("hour_to"),
        "space": devis["space"],
        "reason": f"devis:{devis['devis_reference']}",
    }).execute()


def _unblock_slot(devis: dict) -> None:
    if devis.get("test_mode"):
        return
    sb = _supabase()
    sb.table("cw_blocked_slots") \
        .delete() \
        .eq("reason", f"devis:{devis['devis_reference']}") \
        .execute()


def _get_client_portal_url(email: str) -> Optional[str]:
    """Récupère le lien magic du portail client pour cet email."""
    try:
        sb = _supabase()
        res = sb.table("cw_customers").select("client_token").ilike("email", email).limit(1).execute()
        if res.data and res.data[0].get("client_token"):
            return f"{COWORKING_APP_BASE_URL}/mon-espace/{res.data[0]['client_token']}"
    except Exception as e:
        print(f"[DEVIS] Récupération client_token échouée : {e}")
    return None


def _send_email(devis: dict, subject: str, html_body: str) -> None:
    """
    Envoie un email — en mode TEST, redirige vers l'admin avec marquage explicite.
    """
    if devis.get("test_mode"):
        if not COWORKING_ADMIN_EMAIL:
            print("[DEVIS TEST] COWORKING_ADMIN_EMAIL non configuré, skip email")
            return
        test_banner = (
            '<div style="background:#FFEAA7;border:2px dashed #B88800;padding:12px 16px;'
            'margin:0 0 16px;font-family:Arial,sans-serif;font-size:13px;color:#553C00;'
            'border-radius:4px;">'
            f'<b>🧪 MODE TEST</b> — Cet email aurait été envoyé à : '
            f'<b>{devis.get("email", "?")}</b> ({devis.get("name", "?")})'
            '</div>'
        )
        # Injecte le bandeau juste après <body>
        html_body = html_body.replace("<body", "<body data-test='1'", 1)
        html_body = html_body.replace(
            "</td></tr>\n      <tr><td style=\"padding:32px 28px;\">",
            f"</td></tr>\n      <tr><td style=\"padding:24px 28px 0;\">{test_banner}</td></tr>\n      <tr><td style=\"padding:0 28px 32px;\">",
            1,
        )
        _send_coworking_email(COWORKING_ADMIN_EMAIL, f"[TEST] {subject}", html_body)
        return
    # Mode normal
    _send_coworking_email(devis["email"], subject, html_body)


def _generate_pin_for_devis(devis: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Génère un PIN Igloohome pour la réservation issue d'un devis.
    En mode TEST, retourne un PIN fake `123456` sans appeler Igloohome.
    Retourne (pin_code, pin_id).
    """
    if devis.get("test_mode"):
        return "123456", "test-pin-id"
    if devis["space"] == "Privatisation atelier" or not IGLOOHOME_DEVICE_ID_COWORKING:
        return None, None
    try:
        from pole_sens import igloohome  # type: ignore
        start_dt, end_dt = _compute_datetimes(
            devis["date"], devis.get("slot", "hour"),
            devis.get("hour_from", ""), devis.get("hour_to", "")
        )
        access_name = f"{(devis.get('name') or 'Client')[:30]} {devis['devis_reference']}"[:50]
        pin_start, pin_end = _pin_window(start_dt, end_dt)
        pin_data = igloohome.generate_custom_pin(
            device_id=IGLOOHOME_DEVICE_ID_COWORKING,
            start_date=pin_start,
            end_date=pin_end,
            name=access_name,
        )
        return pin_data.get("pin_code"), pin_data.get("pin_id")
    except Exception as e:
        print(f"[DEVIS] Erreur génération PIN : {e}")
        return None, None


# ============================================================================
# Helpers calcul
# ============================================================================
def compute_totals(items: list[LineItem]) -> dict:
    total_ht = 0.0
    total_tva = 0.0
    tva_buckets: dict[float, float] = {}
    for it in items:
        ligne_ht = it.unit_price_ht * it.quantity
        ligne_tva = ligne_ht * (it.tva_rate / 100.0)
        total_ht += ligne_ht
        total_tva += ligne_tva
        tva_buckets[it.tva_rate] = tva_buckets.get(it.tva_rate, 0.0) + ligne_ht
    return {
        "total_ht": round(total_ht, 2),
        "total_tva": round(total_tva, 2),
        "total_ttc": round(total_ht + total_tva, 2),
        "tva_buckets": {k: round(v, 2) for k, v in tva_buckets.items()},
    }


# ============================================================================
# Générateur PDF Devis
# ============================================================================
def generate_devis_pdf(devis: dict, items: list[dict]) -> bytes:
    """Génère le PDF de devis OU de facture selon le statut du devis.
    - draft / sent      → DEVIS (à valider)
    - validated / acompte_paid / fully_paid → FACTURE (d'acompte, à régler, ou acquittée)
    """
    buf = io.BytesIO()

    # Nature du document selon le statut
    status = devis.get("devis_status", "draft")
    is_facture = status in ("validated", "acompte_paid", "fully_paid")
    validity_days = devis.get("validity_days") or DEFAULT_DEVIS_VALIDITY_DAYS
    _ref_num = devis.get("invoice_acompte_reference") or devis.get("devis_reference", "")
    if is_facture:
        doc_number = _ref_num
        title_txt = "FACTURE"
        if status == "fully_paid":
            badge_txt = "FACTURE ACQUITTÉE"
        elif str(_ref_num).startswith("FAC-AC-"):
            badge_txt = "FACTURE D'ACOMPTE"
        else:
            badge_txt = "FACTURE À RÉGLER"
    else:
        doc_number = devis.get("devis_reference", "")
        title_txt = "DEVIS"
        badge_txt = f"À VALIDER SOUS {validity_days} JOURS"
    _doc_kind = "Facture" if is_facture else "Devis"

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.6*cm, bottomMargin=1.6*cm,
        title=f"{_doc_kind} {doc_number}",
        author=COWORKING_DISPLAY_NAME,
    )

    style_title = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=28,
                                 textColor=ACW_NAVY, spaceAfter=0, leading=32)
    style_subtitle = ParagraphStyle("Sub", fontName="Helvetica", fontSize=9,
                                    textColor=ACW_SLATE, spaceAfter=0, leading=12)
    style_badge = ParagraphStyle("Badge", fontName="Helvetica-Bold", fontSize=8,
                                 textColor=rlcolors.white, alignment=TA_CENTER, leading=12)
    style_h2 = ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=10,
                              textColor=ACW_NAVY, spaceBefore=12, spaceAfter=6, leading=14)
    style_body = ParagraphStyle("Body", fontName="Helvetica", fontSize=9,
                                textColor=ACW_NAVY, leading=12)
    style_legal = ParagraphStyle("Legal", fontName="Helvetica", fontSize=8,
                                 textColor=ACW_SLATE, leading=11)
    style_footer = ParagraphStyle("Footer", fontName="Helvetica", fontSize=7.5,
                                  textColor=ACW_SLATE, alignment=TA_CENTER, leading=11)

    elements: list[Any] = []

    # Header
    logo_bytes = _get_logo_bytes()

    title_block_inner = [
        Paragraph(title_txt, style_title),
        Paragraph("L'ATELIER DU COWORKING", style_subtitle),
        Spacer(1, 8),
    ]
    _badge_bg = ACW_GREEN if status == "fully_paid" else (ACW_NAVY if is_facture else ACW_GOLD)
    badge_table = Table(
        [[Paragraph(badge_txt, style_badge)]],
        colWidths=[5.2*cm],
    )
    badge_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _badge_bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    title_block_inner.append(badge_table)
    title_block = Table([[el] for el in title_block_inner], colWidths=[10*cm])
    title_block.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    if logo_bytes:
        logo_img = Image(io.BytesIO(logo_bytes), width=3.2*cm, height=3.2*cm, kind="proportional")
        header_table = Table([[title_block, logo_img]], colWidths=[10*cm, 7.4*cm])
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (0, 0), "TOP"),
            ("VALIGN", (1, 0), (1, 0), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        elements.append(header_table)
    else:
        elements.append(title_block)
    elements.append(Spacer(1, 16))

    # Infos devis
    validity_str = "—"
    if devis.get("devis_validity_until"):
        try:
            dt = datetime.fromisoformat(str(devis["devis_validity_until"]).replace("Z", "+00:00"))
            validity_str = _format_french_date(dt)
        except Exception:
            validity_str = str(devis["devis_validity_until"])[:10]

    # Date de réservation (prestation) — date + horaires
    resa_str = "—"
    try:
        _d = datetime.strptime(devis.get("date", ""), "%Y-%m-%d")
        resa_str = _format_french_date(_d).capitalize()
    except Exception:
        resa_str = devis.get("date", "") or "—"
    _hf, _ht = devis.get("hour_from"), devis.get("hour_to")
    if _hf and _ht:
        resa_str += f" · {_hf}–{_ht}"

    _num_label = "N° de facture" if is_facture else "N° de devis"
    info_data = [
        [Paragraph(f"<font color='#5A6A85'>{_num_label}</font>", style_body),
         Paragraph(f"<b>{doc_number}</b>", style_body)],
        [Paragraph("<font color='#5A6A85'>Date d'émission</font>", style_body),
         Paragraph(f"<b>{_format_french_date(datetime.now()).capitalize()}</b>", style_body)],
        [Paragraph("<font color='#5A6A85'>Date de réservation</font>", style_body),
         Paragraph(f"<b><font color='#03234D' backColor='#FFF8E6'> {resa_str} </font></b>", style_body)],
    ]
    if not is_facture:
        info_data.append([Paragraph("<font color='#5A6A85'>Valable jusqu'au</font>", style_body),
                          Paragraph(f"<b>{validity_str}</b>", style_body)])
    info_table = Table(info_data, colWidths=[5*cm, 12.4*cm])
    info_table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, ACW_LIGHT_GREY),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 12))

    # Émetteur + Client
    issuer_lines = [
        f"<b>{COWORKING_DISPLAY_NAME}</b>",
        f"<font color='#5A6A85'>({COWORKING_LEGAL_NAME})</font>" if COWORKING_LEGAL_NAME and COWORKING_LEGAL_NAME != COWORKING_DISPLAY_NAME else "",
        COWORKING_ADDRESS_LINE1, COWORKING_ADDRESS_LINE2,
        f"Tél. {COWORKING_PHONE}",
        f"<a href='mailto:{COWORKING_EMAIL}' color='#03234D'>{COWORKING_EMAIL}</a>",
    ]
    if COWORKING_SIRET:
        issuer_lines.append(f"<font size='8' color='#5A6A85'>SIRET : {_format_siret(COWORKING_SIRET)}</font>")
    if COWORKING_VAT_NUMBER:
        issuer_lines.append(f"<font size='8' color='#5A6A85'>TVA intra. : {COWORKING_VAT_NUMBER}</font>")
    issuer_html = "<br/>".join([l for l in issuer_lines if l])

    client_lines = [f"<b>{devis.get('name', '')}</b>"]
    if devis.get("client_type") == "pro" and devis.get("company"):
        client_lines.append(f"<font color='#5A6A85'>{devis['company']}</font>")
    if devis.get("company_address"):
        client_lines.append(devis["company_address"])
    if devis.get("phone"):
        client_lines.append(f"Tél. {devis['phone']}")
    client_lines.append(
        f"<a href='mailto:{devis.get('email', '')}' color='#03234D'>{devis.get('email', '')}</a>"
    )
    if devis.get("company_siret"):
        client_lines.append(f"<font size='8' color='#5A6A85'>SIRET : {_format_siret(devis['company_siret'])}</font>")
    client_html = "<br/>".join(client_lines)

    parties_table = Table([[
        Paragraph(f"<font size='7' color='#5A6A85'>ÉMETTEUR</font><br/><br/>{issuer_html}", style_body),
        Paragraph(f"<font size='7' color='#5A6A85'>FACTURÉ À</font><br/><br/>{client_html}", style_body),
    ]], colWidths=[8.5*cm, 8.9*cm])
    parties_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), ACW_CREAM),
        ("BACKGROUND", (1, 0), (1, 0), ACW_CREAM),
        ("LINEBEFORE", (0, 0), (0, 0), 3, ACW_GOLD),
        ("LINEBEFORE", (1, 0), (1, 0), 3, ACW_GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elements.append(parties_table)
    elements.append(Spacer(1, 14))

    # Mention validation / règlement
    acompte_ratio = devis.get("acompte_ratio") or DEFAULT_ACOMPTE_RATIO
    if is_facture:
        if status == "fully_paid":
            _mention_txt = ("<b>Facture acquittée.</b> Merci pour votre confiance. "
                            "Votre code d'accès vous est communiqué par email séparé.")
        elif str(_ref_num).startswith("FAC-AC-"):
            _mention_txt = (f"<b>Facture d'acompte à régler :</b> merci de régler l'acompte par "
                            f"virement (coordonnées ci-dessous) pour confirmer définitivement votre "
                            f"réservation. Le créneau est d'ores et déjà bloqué en votre nom.")
        else:
            _mention_txt = (f"<b>Facture à régler :</b> merci de régler le montant total par virement "
                            f"(coordonnées ci-dessous). Votre créneau est bloqué en votre nom ; "
                            f"votre code d'accès vous sera communiqué dès réception du règlement.")
    else:
        _mention_txt = (f"<b>Pour valider ce devis :</b> répondez à cet email ou appelez-nous au "
                        f"{COWORKING_PHONE}. À réception de votre accord, une facture d'acompte vous "
                        f"sera envoyée pour régler {int(acompte_ratio * 100)} % du montant total et "
                        f"confirmer définitivement votre réservation.")
    mention = Paragraph(_mention_txt, style_body)
    mention_table = Table([[mention]], colWidths=[17.4*cm])
    mention_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACW_CREAM),
        ("LINEBEFORE", (0, 0), (-1, -1), 3, ACW_NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(mention_table)
    elements.append(Spacer(1, 14))

    # Table des prestations
    header_row = [
        Paragraph("<font color='white' size='8'><b>DESCRIPTION</b></font>", style_body),
        Paragraph("<font color='white' size='8'><b>QTÉ</b></font>", style_body),
        Paragraph("<font color='white' size='8'><b>PU HT</b></font>", style_body),
        Paragraph("<font color='white' size='8'><b>TVA</b></font>", style_body),
        Paragraph("<font color='white' size='8'><b>MONTANT HT</b></font>", style_body),
    ]
    presta_data = [header_row]
    for it in items:
        presta_data.append([
            Paragraph(it["description_html"], style_body),
            Paragraph(f"{it['quantity']}", style_body),
            Paragraph(_format_money(it["unit_price_ht"]), style_body),
            Paragraph(f"{int(it['tva_rate'])} %", style_body),
            Paragraph(_format_money(it["amount_ht"]), style_body),
        ])
    presta_table = Table(presta_data, colWidths=[9.4*cm, 1.4*cm, 2.2*cm, 1.6*cm, 2.8*cm])
    presta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACW_NAVY),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, ACW_LIGHT_GREY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    elements.append(presta_table)
    elements.append(Spacer(1, 12))

    # Totaux
    total_ttc = float(devis.get("amount_total_ttc") or 0)
    total_ht = round(total_ttc / 1.20, 2)  # approximation si TVA mixte → recalcul par item plus précis si besoin
    total_tva = round(total_ttc - total_ht, 2)

    totals_data = [
        [Paragraph("Total HT", style_body), Paragraph(_format_money(total_ht), style_body)],
        [Paragraph("TVA", style_body), Paragraph(_format_money(total_tva), style_body)],
        [Paragraph("<font color='white'><b>TOTAL TTC</b></font>", style_body),
         Paragraph(f"<font color='white'><b>{_format_money(total_ttc)}</b></font>", style_body)],
    ]
    totals_table = Table(totals_data, colWidths=[5*cm, 3.2*cm])
    totals_table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, ACW_LIGHT_GREY),
        ("LINEBELOW", (0, 0), (-1, 1), 0.5, ACW_LIGHT_GREY),
        ("BACKGROUND", (0, 2), (-1, 2), ACW_NAVY),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    totals_wrapper = Table([["", totals_table]], colWidths=[9.2*cm, 8.2*cm])
    totals_wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(totals_wrapper)
    elements.append(Spacer(1, 16))

    # Bloc acompte
    acompte_ttc = float(devis.get("amount_acompte_ttc") or 0)
    solde_ttc = round(total_ttc - acompte_ttc, 2)
    if is_facture and status == "fully_paid":
        acompte_html = (
            f"<b>MODALITÉS DE PAIEMENT</b><br/><br/>"
            f"<font color='#1D9E75'><b>Facture acquittée</b></font> — montant réglé : "
            f"<font size='14'><b>{_format_money(total_ttc)}</b></font> TTC."
        )
    elif is_facture and not str(_ref_num).startswith("FAC-AC-"):
        # Facture "totale" à régler
        acompte_html = (
            f"<b>MODALITÉS DE PAIEMENT</b><br/><br/>"
            f"Montant à régler : "
            f"<font size='14'><b>{_format_money(total_ttc)}</b></font> TTC "
            f"<font color='#5A6A85' size='8'>par virement (coordonnées ci-dessous)</font><br/>"
            f"<font color='#5A6A85' size='8'>Votre créneau est bloqué en votre nom. "
            f"Le code d'accès vous sera communiqué dès réception du règlement.</font>"
        )
    else:
        # Devis, ou facture d'acompte
        _lead = "Acompte à régler" if is_facture else "Acompte demandé à la validation"
        acompte_html = (
            f"<b>MODALITÉS DE PAIEMENT</b><br/><br/>"
            f"{_lead} : "
            f"<font size='14'><b>{_format_money(acompte_ttc)}</b></font> "
            f"<font color='#5A6A85' size='8'>({int(acompte_ratio * 100)} % du total TTC)</font><br/><br/>"
            f"<b>Solde à régler avant la prestation :</b> {_format_money(solde_ttc)}<br/>"
            f"<font color='#5A6A85' size='8'>Le créneau sera bloqué dès validation de votre devis. "
            f"La réservation sera définitivement confirmée à réception de l'acompte.</font>"
        )
    acompte_block = Paragraph(acompte_html, style_body)
    acompte_wrap = Table([[acompte_block]], colWidths=[17.4*cm])
    acompte_wrap.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), rlcolors.HexColor("#FFF8E6")),
        ("BOX", (0, 0), (-1, -1), 1, ACW_GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 16), ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    elements.append(acompte_wrap)
    elements.append(Spacer(1, 14))

    # Bloc RIB
    if COWORKING_IBAN:
        rib_lines = ["<b>Coordonnées bancaires pour le virement :</b>"]
        if COWORKING_BANK_NAME:
            rib_lines.append(f"Banque : {COWORKING_BANK_NAME}")
        rib_lines.append(f"IBAN : <font face='Courier'>{COWORKING_IBAN}</font>")
        if COWORKING_BIC:
            rib_lines.append(f"BIC : <font face='Courier'>{COWORKING_BIC}</font>")
        rib_lines.append(
            f"<font color='#5A6A85'>Merci de rappeler la référence "
            f"<b>{doc_number}</b> dans le libellé du virement.</font>"
        )
        rib_block = Paragraph("<br/>".join(rib_lines), style_body)
        rib_wrap = Table([[rib_block]], colWidths=[17.4*cm])
        rib_wrap.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ACW_CREAM),
            ("BOX", (0, 0), (-1, -1), 0.5, ACW_GOLD),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        elements.append(rib_wrap)
        elements.append(Spacer(1, 14))

    # Mentions légales — adaptées au type de document
    if is_facture:
        cg_title = "Conditions & mentions légales"
        mentions = [
            "• Facture payable à réception, par virement (coordonnées ci-dessus).",
            "• Pas d'escompte accordé pour paiement anticipé.",
            "• En cas de retard de paiement : pénalité égale à 3 fois le taux d'intérêt légal en vigueur.",
            "• Indemnité forfaitaire pour frais de recouvrement : 40 € (art. L441-10 du Code de commerce).",
            "• Réservation définitivement confirmée à réception du règlement ; code d'accès communiqué ensuite.",
            "• Annulation : remboursement intégral si > 14j avant, 50 % entre 7 et 14j, aucun < 7j.",
            "• Les frais bancaires liés au paiement ne sont pas remboursables et sont déduits du montant remboursé.",
            "• TVA acquittée sur les encaissements.",
        ]
    else:
        cg_title = "Conditions générales"
        mentions = [
            f"• Devis valable {validity_days} jours à compter de la date d'émission.",
            "• Acceptation par retour d'email, téléphone ou signature manuscrite.",
            f"• Réservation confirmée à réception de l'acompte ({int(acompte_ratio * 100)} %) ou du total.",
            "• Solde à régler au plus tard 7 jours avant la prestation.",
            "• Annulation : remboursement intégral si > 14j avant, 50 % entre 7 et 14j, aucun < 7j.",
            "• Dans tous les cas, les frais bancaires liés au paiement (commission Stripe) ne sont pas remboursables et sont déduits du montant remboursé.",
            "• TVA acquittée sur les encaissements.",
        ]
    elements.append(Paragraph(f"<b>{cg_title}</b>", style_h2))
    elements.append(Paragraph("<br/>".join(mentions), style_legal))
    elements.append(Spacer(1, 20))

    # Footer
    footer_lines = [
        f"{COWORKING_DISPLAY_NAME} — {COWORKING_LEGAL_NAME}"
        if COWORKING_LEGAL_NAME and COWORKING_LEGAL_NAME != COWORKING_DISPLAY_NAME
        else COWORKING_DISPLAY_NAME,
        f"{COWORKING_ADDRESS_LINE1} · {COWORKING_ADDRESS_LINE2}",
    ]
    footer_legal = []
    if COWORKING_SIRET:
        footer_legal.append(f"SIRET {_format_siret(COWORKING_SIRET)}")
    if COWORKING_VAT_NUMBER:
        footer_legal.append(f"TVA {COWORKING_VAT_NUMBER}")
    if footer_legal:
        footer_lines.append(" · ".join(footer_legal))
    footer_lines.append(f"<a href='https://{COWORKING_WEBSITE}' color='#C9B584'>{COWORKING_WEBSITE}</a>")
    footer_table = Table(
        [[Paragraph("<br/>".join(footer_lines), style_footer)]],
        colWidths=[17.4*cm],
    )
    footer_table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.5, ACW_LIGHT_GREY),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(footer_table)

    doc.build(elements)
    return buf.getvalue()


# ============================================================================
# Endpoints
# ============================================================================

# ----------------------------------------------------------------------------
# Helpers mot de passe admin (PBKDF2 SHA-256, identiques à l'auth client)
# ----------------------------------------------------------------------------
def _admin_hash_password(password: str, salt: Optional[str] = None) -> tuple:
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000
    ).hex()
    return pwd_hash, salt


def _admin_verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not salt or not expected_hash:
        return False
    pwd_hash, _ = _admin_hash_password(password, salt)
    return secrets.compare_digest(pwd_hash, expected_hash)


def _fetch_admin_row(email: str) -> Optional[dict]:
    """Récupère la ligne cw_admins pour l'email (None si table absente ou ligne inexistante)."""
    try:
        sb = _supabase()
        res = sb.table("cw_admins").select("*").ilike("email", email.strip().lower()).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f"[ADMIN AUTH] cw_admins indisponible : {e}")
        return None


def _send_admin_reset_email(to_email: str, reset_token: str) -> None:
    reset_url = f"{COWORKING_APP_BASE_URL}/admin-reset-password.html?token={reset_token}"
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>David</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Une réinitialisation du mot de passe <b>administrateur</b> de L'Atelier du Coworking a été demandée.
      Cliquez sur le bouton ci-dessous pour choisir un nouveau mot de passe (lien valable 1 heure).
    </p>
    {_btn_pdf("Réinitialiser le mot de passe admin", reset_url)}
    <p style="margin:0 0 16px;font-size:13px;line-height:1.7;color:#5A6A85;">
      Si vous n'êtes pas à l'origine de cette demande, ignorez cet email : votre mot de passe reste inchangé.
    </p>
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">L'Atelier du Coworking</p>
    """
    _send_coworking_email(to_email, "Réinitialisation du mot de passe admin — L'Atelier du Coworking",
                          _email_shell("L'Atelier du Coworking", "Sécurité — accès admin", body))


@router.post("/admin/login", response_model=LoginResponse)
def admin_login(payload: LoginRequest):
    """
    Login admin coworking.
    Vérifie d'abord un mot de passe enregistré en base (cw_admins) ; à défaut,
    retombe sur la variable d'environnement COWORKING_ADMIN_PASSWORD.
    """
    if not COWORKING_ADMIN_EMAIL:
        raise HTTPException(500, "Authentification admin non configurée (COWORKING_ADMIN_EMAIL manquant)")
    if not COWORKING_ADMIN_TOKEN:
        raise HTTPException(500, "COWORKING_ADMIN_TOKEN non configuré")

    if payload.email.strip().lower() != COWORKING_ADMIN_EMAIL.strip().lower():
        raise HTTPException(401, "Email ou mot de passe incorrect")

    ok = False
    row = _fetch_admin_row(COWORKING_ADMIN_EMAIL)
    if row and row.get("password_hash") and row.get("password_salt"):
        # Mot de passe défini en base → fait foi
        ok = _admin_verify_password(payload.password, row["password_salt"], row["password_hash"])
    elif COWORKING_ADMIN_PASSWORD:
        # Pas encore de mot de passe en base → fallback env var
        ok = secrets.compare_digest(payload.password, COWORKING_ADMIN_PASSWORD)

    if not ok:
        raise HTTPException(401, "Email ou mot de passe incorrect")

    return LoginResponse(token=COWORKING_ADMIN_TOKEN, email=COWORKING_ADMIN_EMAIL)


class AdminForgotRequest(BaseModel):
    email: str


class AdminResetRequest(BaseModel):
    token: str
    new_password: str


@router.post("/admin/forgot-password")
def admin_forgot_password(payload: AdminForgotRequest):
    """Envoie un lien de réinitialisation à l'email de secours admin (valable 1h).
    Réponse toujours 200 (anti-énumération)."""
    generic = {"ok": True, "message": "Si l'adresse correspond au compte administrateur, un email de réinitialisation a été envoyé."}
    if not COWORKING_ADMIN_EMAIL:
        return generic
    if payload.email.strip().lower() != COWORKING_ADMIN_EMAIL.strip().lower():
        return generic
    try:
        sb = _supabase()
        reset_token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"
        row = _fetch_admin_row(COWORKING_ADMIN_EMAIL)
        recovery = (row.get("recovery_email") if row else None) or COWORKING_ADMIN_RECOVERY_EMAIL
        if row:
            sb.table("cw_admins").update({
                "password_reset_token": reset_token,
                "password_reset_expires_at": expires,
            }).ilike("email", COWORKING_ADMIN_EMAIL.strip().lower()).execute()
        else:
            sb.table("cw_admins").insert({
                "email": COWORKING_ADMIN_EMAIL.strip().lower(),
                "recovery_email": recovery,
                "password_reset_token": reset_token,
                "password_reset_expires_at": expires,
            }).execute()
        _send_admin_reset_email(recovery, reset_token)
    except Exception as e:
        print(f"[ADMIN AUTH] forgot-password erreur : {e}")
    return generic


@router.post("/admin/reset-password")
def admin_reset_password(payload: AdminResetRequest):
    """Réinitialise le mot de passe admin à partir du token reçu par email."""
    if len(payload.new_password) < 8 or payload.new_password.lower() == payload.new_password or not any(c.isdigit() for c in payload.new_password):
        raise HTTPException(400, "Mot de passe trop faible : 8 caractères minimum, au moins une majuscule et un chiffre.")
    sb = _supabase()
    try:
        res = sb.table("cw_admins").select("*").eq("password_reset_token", payload.token).limit(1).execute()
    except Exception:
        raise HTTPException(500, "Table cw_admins indisponible — migration SQL non exécutée ?")
    if not res.data:
        raise HTTPException(400, "Lien de réinitialisation invalide.")
    row = res.data[0]
    expires_str = row.get("password_reset_expires_at")
    if expires_str:
        try:
            expires = datetime.fromisoformat(str(expires_str).replace("Z", "+00:00"))
            if expires.replace(tzinfo=None) < datetime.utcnow():
                raise HTTPException(400, "Lien de réinitialisation expiré.")
        except ValueError:
            raise HTTPException(400, "Lien invalide.")
    pwd_hash, salt = _admin_hash_password(payload.new_password)
    sb.table("cw_admins").update({
        "password_hash": pwd_hash,
        "password_salt": salt,
        "password_reset_token": None,
        "password_reset_expires_at": None,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }).eq("id", row["id"]).execute()
    return {"ok": True, "message": "Mot de passe réinitialisé. Vous pouvez vous connecter."}


def _hm_to_minutes(hm: str) -> int:
    """'09:30' → 570"""
    try:
        h, m = hm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


@router.post("/devis", response_model=DevisResponse)
def create_devis(payload: DevisCreateRequest, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)

    # Validation : privatisation = minimum 3 heures
    if payload.space == "privatisation":
        duration_min = _hm_to_minutes(payload.hour_to) - _hm_to_minutes(payload.hour_from)
        if duration_min < 180:  # 180 min = 3h
            raise HTTPException(
                400,
                f"Privatisation : minimum 3 heures requis. "
                f"Vous avez demandé {duration_min // 60}h{duration_min % 60:02d}. "
                f"Élargissez le créneau ({payload.hour_from} → {payload.hour_to})."
            )

    # Validation : capacité de l'espace + privatisation exclusive
    # Enforced même en mode TEST (test devis comptés vs test devis uniquement, isolé de la prod)
    # Bypass UNIQUEMENT via force_create (admin override exceptionnel)
    date_iso = payload.date.isoformat()
    is_test = payload.test_mode
    test_prefix = "[TEST] " if is_test else ""
    skip_validation = payload.force_create

    if not skip_validation:
        # Cas 1 : une privatisation est déjà réservée → bloque tout
        if payload.space != "privatisation" and _has_privatisation_conflict(date_iso, payload.hour_from, payload.hour_to, is_test):
            raise HTTPException(
                409,
                f"Une privatisation de l'atelier est déjà réservée sur ce créneau — "
                f"aucune autre réservation possible. Choisis un autre horaire."
            )
        # Cas 2 : si tu réserves une privatisation, il ne doit y avoir aucune autre réservation
        if payload.space == "privatisation":
            for other_space in ("coworking", "bureau", "salle-reunion"):
                if _count_overlapping_reservations(other_space, date_iso, payload.hour_from, payload.hour_to, is_test) > 0:
                    raise HTTPException(
                        409,
                        f"Impossible de privatiser : il y a déjà une réservation {other_space} sur ce créneau. "
                        f"Annule ou décale d'abord les réservations existantes."
                    )
        # Cas 3 : vérification capacité de l'espace + conflit d'unité spécifique
        else:
            try:
                sb = _supabase()
                space_row = sb.table("cw_spaces").select("capacity,name").eq("slug", payload.space).limit(1).execute()
                capacity = (space_row.data[0]["capacity"] if space_row.data else 1)
                space_name = (space_row.data[0]["name"] if space_row.data else payload.space)
            except Exception:
                capacity = 1
                space_name = payload.space
            count = _count_overlapping_reservations(payload.space, date_iso, payload.hour_from, payload.hour_to, is_test)
            if count >= capacity:
                raise HTTPException(
                    409,
                    f"Capacité atteinte pour {space_name} sur ce créneau : "
                    f"{count} réservation(s) active(s) sur {capacity} place(s) disponibles."
                )
            # Vérif conflit d'unité spécifique (Bureau 1, Poste 3, etc.)
            if payload.space_unit:
                sb = _supabase()
                res_unit = sb.table("cw_reservations") \
                    .select("hour_from,hour_to,space_unit,status,devis_status,name") \
                    .eq("date", date_iso) \
                    .eq("test_mode", is_test) \
                    .eq("space_unit", payload.space_unit) \
                    .execute()
                for r in res_unit.data or []:
                    active = (r.get("status") == "confirmed") or (r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"))
                    if not active:
                        continue
                    if _intervals_overlap(payload.hour_from, payload.hour_to, r.get("hour_from") or "08:00", r.get("hour_to") or "18:00"):
                        raise HTTPException(
                            409,
                            f"{payload.space_unit} est déjà réservé sur ce créneau par {r.get('name', '?')}."
                        )

    totals = compute_totals(payload.items)
    acompte_ttc = round(totals["total_ttc"] * payload.acompte_ratio, 2)
    devis_ref = _generate_next_devis_reference()
    # En mode TEST, on préfixe la référence pour qu'elle soit visiblement à part
    if payload.test_mode:
        devis_ref = f"TEST-{devis_ref}"
    validity_until = datetime.utcnow() + timedelta(days=payload.validity_days)

    reservation_ref = devis_ref.replace("DEV-", "RES-")

    body = {
        "reference": reservation_ref,
        "devis_reference": devis_ref,
        "devis_status": "draft",
        "payment_mode": "virement",
        "status": "pending_devis",
        "test_mode": payload.test_mode,
        "name": payload.client_name,
        "email": payload.client_email,
        "phone": payload.client_phone,
        "client_type": payload.client_type,
        "company": payload.company,
        "company_siret": payload.company_siret,
        "company_address": payload.company_address,
        "date": payload.date.isoformat(),
        "hour_from": payload.hour_from,
        "hour_to": payload.hour_to,
        "slot": payload.slot,
        "space": payload.space,
        "space_unit": payload.space_unit,
        "amount_ttc": totals["total_ttc"],          # compat colonne existante
        "amount_total_ttc": totals["total_ttc"],
        "amount_acompte_ttc": acompte_ttc,
        "amount_paid_ttc": 0,
        "devis_validity_until": validity_until.isoformat() + "Z",
        "admin_notes": payload.admin_notes,
        "items_json": [it.dict() for it in payload.items],
    }
    sb = _supabase()
    res = sb.table("cw_reservations").insert(body).execute()
    if not res.data:
        raise HTTPException(500, "Échec création devis")
    row = res.data[0]

    # Upsert client dans cw_customers (sauf en mode TEST pour ne pas polluer la base)
    if not payload.test_mode:
        try:
            sb.table("cw_customers").upsert({
                "email": payload.client_email,
                "name": payload.client_name,
                "phone": payload.client_phone,
                "company": payload.company,
                "company_siret": payload.company_siret,
                "company_address": payload.company_address,
                "client_type": payload.client_type,
                "last_booking_at": datetime.utcnow().isoformat() + "Z",
            }, on_conflict="email").execute()
        except Exception as e:
            print(f"[DEVIS] Upsert client échoué (non bloquant) : {e}")

    return DevisResponse(
        id=str(row["id"]),
        devis_reference=row["devis_reference"],
        status=row["devis_status"],
        amount_total_ttc=row["amount_total_ttc"],
        amount_acompte_ttc=row["amount_acompte_ttc"],
    )


@router.get("/devis/{devis_id}.pdf")
def get_devis_pdf(devis_id: str):
    """PDF du devis — accessible client via lien email (pas de token requis)."""
    devis = _fetch_devis(devis_id)
    if not devis or not devis.get("devis_reference"):
        raise HTTPException(404, "Devis non trouvé")

    items_raw = devis.get("items_json") or []
    pdf_items = []
    for it in items_raw:
        ligne_ht = it["unit_price_ht"] * it["quantity"]
        pdf_items.append({
            "description_html": f"<b>{it['description']}</b>",
            "quantity": it["quantity"],
            "unit_price_ht": it["unit_price_ht"],
            "tva_rate": it["tva_rate"],
            "amount_ht": ligne_ht,
        })
    pdf_bytes = generate_devis_pdf(devis, pdf_items)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{devis["devis_reference"]}.pdf"'},
    )


@router.post("/devis/{devis_id}/send")
def send_devis(devis_id: str, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") not in ("draft", "sent"):
        raise HTTPException(400, f"Statut non éligible : {devis.get('devis_status')}")

    devis_pdf_url = f"{COWORKING_APP_BASE_URL}/api/coworking/devis/{devis_id}.pdf"
    subject = f"Votre devis — L'Atelier du Coworking — {devis['devis_reference']}"
    html_body = _render_email_devis(devis, devis_pdf_url)
    _send_email(devis, subject, html_body)

    _update_devis(devis_id, {
        "devis_status": "sent",
        "devis_sent_at": datetime.utcnow().isoformat() + "Z",
    })
    return {"ok": True, "status": "sent"}


@router.post("/devis/{devis_id}/validate")
def validate_devis(
    devis_id: str,
    mode: str = Query("acompte", description="acompte = demander l'acompte (%) ; total = demander le règlement total"),
    authorization: Optional[str] = Header(None),
):
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") != "sent":
        raise HTTPException(400, f"Statut invalide : {devis.get('devis_status')}")

    _block_slot(devis)
    invoice_ref = devis["devis_reference"].replace(
        "DEV-", "FAC-" if mode == "total" else "FAC-AC-"
    )
    _update_devis(devis_id, {
        "devis_status": "validated",
        "devis_validated_at": datetime.utcnow().isoformat() + "Z",
        "invoice_acompte_reference": invoice_ref,
    })

    devis = _fetch_devis(devis_id)
    pdf_url = f"{COWORKING_APP_BASE_URL}/api/coworking/devis/{devis_id}.pdf"
    if mode == "total":
        subject = f"Facture — Confirmez votre réservation — {devis['devis_reference']}"
        html_body = _render_email_total_demande(devis, pdf_url)
    else:
        subject = f"Facture d'acompte — Confirmez votre réservation — {devis['devis_reference']}"
        html_body = _render_email_acompte_demande(devis, pdf_url)
    _send_email(devis, subject, html_body)

    return {"ok": True, "status": "validated", "mode": mode, "invoice_reference": invoice_ref}


@router.post("/devis/{devis_id}/mark-acompte")
def mark_acompte(devis_id: str, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") != "validated":
        raise HTTPException(400, f"Statut invalide : {devis.get('devis_status')}")

    acompte_ttc = float(devis.get("amount_acompte_ttc") or 0)
    new_paid = float(devis.get("amount_paid_ttc") or 0) + acompte_ttc
    # Réutilise le PIN s'il a déjà été généré (ex. accès envoyé avant paiement)
    pin_code = devis.get("pin_code")
    pin_id = devis.get("pin_id")
    if not pin_code:
        pin_code, pin_id = _generate_pin_for_devis(devis)

    _update_devis(devis_id, {
        "devis_status": "acompte_paid",
        "acompte_received_at": datetime.utcnow().isoformat() + "Z",
        "amount_paid_ttc": new_paid,
        "pin_code": pin_code,
        "pin_id": pin_id,
        "status": "confirmed",
    })

    devis = _fetch_devis(devis_id)
    subject = f"Réservation confirmée — Voici votre code d'accès — {devis['devis_reference']}"
    html_body = _render_email_confirmation_acompte(devis, pin_code)
    _send_email(devis, subject, html_body)
    return {"ok": True, "status": "acompte_paid", "pin": pin_code}


@router.post("/devis/{devis_id}/send-access")
def send_access(devis_id: str, authorization: Optional[str] = Header(None)):
    """Envoie le code d'accès au client DÈS la validation, sans attendre le paiement.
    Génère le PIN si besoin, confirme la réservation, envoie l'email d'accès.
    Le statut de paiement (attente acompte) n'est pas modifié."""
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") not in ("validated", "acompte_paid", "fully_paid"):
        raise HTTPException(400, "Le devis doit d'abord être validé avant d'envoyer les accès.")

    # Réutilise le PIN existant, sinon en génère un
    pin_code = devis.get("pin_code")
    pin_id = devis.get("pin_id")
    if not pin_code:
        pin_code, pin_id = _generate_pin_for_devis(devis)

    _update_devis(devis_id, {
        "pin_code": pin_code,
        "pin_id": pin_id,
        "status": "confirmed",
    })

    devis = _fetch_devis(devis_id)
    subject = f"Vos accès — L'Atelier du Coworking — {devis['devis_reference']}"
    html_body = _render_email_access(devis, pin_code)
    _send_email(devis, subject, html_body)
    return {"ok": True, "pin": pin_code}


@router.post("/devis/{devis_id}/mark-solde")
def mark_solde(devis_id: str, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") != "acompte_paid":
        raise HTTPException(400, f"Statut invalide : {devis.get('devis_status')}")

    total_ttc = float(devis.get("amount_total_ttc") or 0)
    paid_ttc = float(devis.get("amount_paid_ttc") or 0)
    solde_ttc = round(total_ttc - paid_ttc, 2)
    invoice_solde_ref = devis["devis_reference"].replace("DEV-", "FAC-SO-")

    _update_devis(devis_id, {
        "devis_status": "fully_paid",
        "solde_received_at": datetime.utcnow().isoformat() + "Z",
        "amount_paid_ttc": total_ttc,
        "invoice_solde_reference": invoice_solde_ref,
    })

    devis = _fetch_devis(devis_id)
    subject = f"Solde reçu — Dossier complet — {devis['devis_reference']}"
    html_body = _render_email_solde_recu(devis, solde_ttc)
    _send_email(devis, subject, html_body)
    return {"ok": True, "status": "fully_paid", "invoice_solde_reference": invoice_solde_ref}


@router.post("/devis/{devis_id}/mark-total")
def mark_total(devis_id: str, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") not in ("sent", "validated"):
        raise HTTPException(400, f"Statut invalide : {devis.get('devis_status')}")

    if devis.get("devis_status") == "sent":
        _block_slot(devis)

    pin_code, pin_id = _generate_pin_for_devis(devis)
    invoice_ref = devis["devis_reference"].replace("DEV-", "FAC-")
    total_ttc = float(devis.get("amount_total_ttc") or 0)
    now_iso = datetime.utcnow().isoformat() + "Z"

    _update_devis(devis_id, {
        "devis_status": "fully_paid",
        "devis_validated_at": devis.get("devis_validated_at") or now_iso,
        "acompte_received_at": now_iso,
        "solde_received_at": now_iso,
        "amount_paid_ttc": total_ttc,
        "pin_code": pin_code,
        "pin_id": pin_id,
        "reference": invoice_ref,
        "status": "confirmed",
    })

    devis = _fetch_devis(devis_id)
    subject = f"Réservation confirmée — {invoice_ref}"
    html_body = _render_email_confirmation_total(devis, pin_code)
    _send_email(devis, subject, html_body)
    return {"ok": True, "status": "fully_paid", "invoice_reference": invoice_ref}


@router.post("/devis/{devis_id}/send-facture-acquittee")
def send_facture_acquittee(devis_id: str, authorization: Optional[str] = Header(None)):
    """Envoie au client la FACTURE ACQUITTÉE (une fois le paiement total enregistré)."""
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") != "fully_paid":
        raise HTTPException(400, "La facture n'est acquittée qu'une fois le règlement total enregistré.")

    pdf_url = f"{COWORKING_APP_BASE_URL}/api/coworking/devis/{devis_id}.pdf"
    subject = f"Facture acquittée — {devis['devis_reference']}"
    html_body = _render_email_facture_acquittee(devis, pdf_url)
    _send_email(devis, subject, html_body)
    return {"ok": True}


@router.post("/devis/{devis_id}/duplicate", response_model=DevisResponse)
def duplicate_devis(
    devis_id: str,
    new_date: Optional[str] = Query(None, description="Nouvelle date YYYY-MM-DD (défaut : même date que l'original)"),
    authorization: Optional[str] = Header(None),
):
    """
    Duplique un devis : crée un nouveau brouillon avec les mêmes infos.
    Optionnel : `new_date` pour décaler la prestation.
    """
    _check_admin(authorization)
    src = _fetch_devis(devis_id)
    if not src:
        raise HTTPException(404, "Devis source non trouvé")

    # Nouvelle référence
    new_ref = _generate_next_devis_reference()
    if src.get("test_mode"):
        new_ref = f"TEST-{new_ref}"
    new_reservation_ref = new_ref.replace("DEV-", "RES-")

    # Date prestation : par défaut la même, sinon celle passée en paramètre
    target_date = new_date or src.get("date")

    validity_until = datetime.utcnow() + timedelta(days=DEFAULT_DEVIS_VALIDITY_DAYS)

    body = {
        "reference": new_reservation_ref,
        "devis_reference": new_ref,
        "devis_status": "draft",
        "payment_mode": "virement",
        "status": "pending_devis",
        "test_mode": bool(src.get("test_mode")),
        "name": src.get("name"),
        "email": src.get("email"),
        "phone": src.get("phone"),
        "client_type": src.get("client_type"),
        "company": src.get("company"),
        "company_siret": src.get("company_siret"),
        "company_address": src.get("company_address"),
        "date": target_date,
        "hour_from": src.get("hour_from"),
        "hour_to": src.get("hour_to"),
        "slot": src.get("slot"),
        "space": src.get("space"),
        "space_unit": src.get("space_unit"),
        "amount_ttc": src.get("amount_total_ttc") or src.get("amount_ttc"),
        "amount_total_ttc": src.get("amount_total_ttc"),
        "amount_acompte_ttc": src.get("amount_acompte_ttc"),
        "amount_paid_ttc": 0,
        "devis_validity_until": validity_until.isoformat() + "Z",
        "admin_notes": f"Dupliqué depuis {src.get('devis_reference')}",
        "items_json": src.get("items_json"),
    }
    sb = _supabase()
    res = sb.table("cw_reservations").insert(body).execute()
    if not res.data:
        raise HTTPException(500, "Échec duplication")
    row = res.data[0]
    return DevisResponse(
        id=str(row["id"]),
        devis_reference=row["devis_reference"],
        status=row["devis_status"],
        amount_total_ttc=row["amount_total_ttc"],
        amount_acompte_ttc=row["amount_acompte_ttc"],
    )


@router.delete("/devis/{devis_id}")
def delete_devis(devis_id: str, authorization: Optional[str] = Header(None)):
    """
    Supprime définitivement un devis et libère son slot si bloqué.
    À utiliser avec parcimonie — préférer 'cancel' pour garder une trace.
    """
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    # Libère le slot si bloqué
    if devis.get("devis_status") in ("validated", "acompte_paid"):
        try:
            _unblock_slot(devis)
        except Exception as e:
            print(f"[DEVIS DELETE] Unblock slot échoué (non bloquant) : {e}")
    sb = _supabase()
    sb.table("cw_reservations").delete().eq("id", devis_id).execute()
    return {"ok": True, "deleted": devis_id}


@router.post("/devis/{devis_id}/cancel")
def cancel_devis(devis_id: str, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    devis = _fetch_devis(devis_id)
    if not devis:
        raise HTTPException(404, "Devis non trouvé")
    if devis.get("devis_status") in ("validated", "acompte_paid"):
        _unblock_slot(devis)
    _update_devis(devis_id, {
        "devis_status": "cancelled",
        "status": "cancelled",
    })
    return {"ok": True, "status": "cancelled"}


@router.get("/spaces")
def list_spaces(authorization: Optional[str] = Header(None)):
    """Liste tous les espaces avec leur capacité."""
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_spaces").select("*").eq("active", True).order("sort_order").execute()
    return {"spaces": res.data or [], "count": len(res.data or [])}


# ============================================================================
# Fermetures (cw_blocked_slots) — gestion admin (congés, jours fériés…)
# ============================================================================
class ClosureCreate(BaseModel):
    date: str                         # YYYY-MM-DD
    space: Optional[str] = None       # None/"" = tout l'atelier ; sinon slug (coworking|bureau|salle-reunion)
    hour_from: Optional[str] = None   # None = toute la journée
    hour_to: Optional[str] = None
    reason: Optional[str] = None


@router.get("/admin/closures")
def list_closures(authorization: Optional[str] = Header(None)):
    """Liste les fermetures (manuelles + celles liées aux devis, marquées)."""
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_blocked_slots").select("*").order("date").execute()
    out = []
    for b in res.data or []:
        reason = b.get("reason") or ""
        out.append({**b, "is_devis": reason.startswith("devis:")})
    return {"closures": out}


@router.post("/admin/closures")
def create_closure(payload: ClosureCreate, authorization: Optional[str] = Header(None)):
    """Crée une fermeture manuelle."""
    _check_admin(authorization)
    if not payload.date:
        raise HTTPException(400, "Date requise")
    sb = _supabase()
    row = {
        "date": payload.date,
        "space": (payload.space or None),
        "hour_from": (payload.hour_from or None),
        "hour_to": (payload.hour_to or None),
        "reason": (payload.reason or "Fermeture"),
    }
    res = sb.table("cw_blocked_slots").insert(row).execute()
    return {"ok": True, "closure": res.data[0] if res.data else None}


@router.delete("/admin/closures/{closure_id}")
def delete_closure(closure_id: str, authorization: Optional[str] = Header(None)):
    """Supprime une fermeture (rouvre la date). Refuse les blocages liés à un devis."""
    _check_admin(authorization)
    sb = _supabase()
    cur = sb.table("cw_blocked_slots").select("reason").eq("id", closure_id).limit(1).execute()
    if cur.data and (cur.data[0].get("reason") or "").startswith("devis:"):
        raise HTTPException(400, "Cette fermeture est liée à un devis : annulez le devis correspondant.")
    sb.table("cw_blocked_slots").delete().eq("id", closure_id).execute()
    return {"ok": True}


# ============================================================================
# Statistiques & export comptable (admin)
# ============================================================================
def _reservation_is_active(r: dict) -> bool:
    return (r.get("status") == "confirmed") or (r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"))


def _slot_day_weight(r: dict) -> float:
    """Poids d'occupation d'une réservation en équivalent-jour (journée=1)."""
    slot = (r.get("slot") or "").lower()
    if slot in ("morning", "afternoon", "half-day-morning", "half-day-afternoon"):
        return 0.5
    if slot == "hour":
        try:
            hf = _hm_to_minutes(r.get("hour_from") or "08:00")
            ht = _hm_to_minutes(r.get("hour_to") or "18:00")
            return max((ht - hf) / 600.0, 0.1)  # /600 min = journée de 10h
        except Exception:
            return 0.2
    return 1.0  # journée / défaut


def _payment_label(r: dict) -> str:
    pm = (r.get("payment_method") or "").lower()
    if pm == "pack":
        return "Forfait"
    if pm == "stripe":
        return "Carte (Stripe)"
    if r.get("devis_status"):
        return "Virement (devis)"
    return "Carte (Stripe)"


def _category_label(r: dict) -> str:
    """Libellé de catégorie (Bureau 1, Salle de réunion…) pour les stats."""
    u = (r.get("space_unit") or "").strip()
    if u:
        return u
    s = (r.get("space") or "").strip()
    m = {
        "bureau-1": "Bureau 1", "bureau-2": "Bureau 2",
        "salle-reunion": "Salle de réunion", "salle de réunion": "Salle de réunion",
        "coworking": "Espace coworking", "espace coworking": "Espace coworking",
        "privatisation": "Privatisation atelier", "privatisation atelier": "Privatisation atelier",
    }
    return m.get(s.lower(), s or "—")


def _duration_label(r: dict) -> str:
    """Libellé de durée/offre (Par heure, Demi-journée, Journée, Forfait)."""
    if (r.get("payment_method") or "").lower() == "pack":
        return "Forfait"
    s = (r.get("slot") or "").lower()
    return {
        "hour": "Par heure",
        "morning": "Demi-journée", "afternoon": "Demi-journée",
        "half-day-morning": "Demi-journée", "half-day-afternoon": "Demi-journée",
        "day": "Journée", "week": "Forfait", "month": "Forfait", "pack": "Forfait",
    }.get(s, s or "—")


_CATEGORY_CAPACITY = {
    "Bureau 1": 1, "Bureau 2": 1, "Salle de réunion": 1,
    "Espace coworking": 6, "Privatisation atelier": 1,
}


def _open_days(d_from: str, d_to: str) -> int:
    """Nombre de jours ouvrés (lun-ven) inclus dans [d_from, d_to]."""
    from datetime import date as _date
    try:
        d0 = _date.fromisoformat(d_from); d1 = _date.fromisoformat(d_to)
        return sum(1 for n in range((d1 - d0).days + 1)
                   if _date.fromordinal(d0.toordinal() + n).weekday() < 5)
    except Exception:
        return 0


def _aggregate_stats(rows: list, open_days: int) -> dict:
    """Agrège un ensemble de réservations actives en métriques de tableau de bord."""
    total_ca = sum(float(r.get("amount_ttc") or 0) for r in rows)
    count = len(rows)
    by_space, by_month, by_pay, by_client = {}, {}, {}, {}
    by_offer, by_cat, cat_weight = {}, {}, {}
    booked_weight = 0.0
    for r in rows:
        slug = _norm_space_slug(r.get("space"))
        amt = float(r.get("amount_ttc") or 0)
        ht = amt / 1.20
        by_space[slug] = by_space.get(slug, 0.0) + amt
        m = (r.get("date") or "")[:7]
        by_month[m] = by_month.get(m, 0.0) + amt
        pl = _payment_label(r)
        by_pay[pl] = by_pay.get(pl, 0.0) + amt
        em = r.get("email") or "—"
        c = by_client.setdefault(em, {"email": em, "name": r.get("name"), "ca": 0.0, "count": 0})
        c["ca"] += amt; c["count"] += 1
        w = _slot_day_weight(r)
        booked_weight += w
        cat = _category_label(r)
        cd = by_cat.setdefault(cat, {"category": cat, "ca": 0.0, "ca_ht": 0.0, "count": 0})
        cd["ca"] += amt; cd["ca_ht"] += ht; cd["count"] += 1
        cat_weight[cat] = cat_weight.get(cat, 0.0) + w
        off = f"{cat} — {_duration_label(r)}"
        od = by_offer.setdefault(off, {"offer": off, "ca": 0.0, "ca_ht": 0.0, "count": 0})
        od["ca"] += amt; od["ca_ht"] += ht; od["count"] += 1

    CAPACITY_UNITS = 9  # Bureau 1 + Bureau 2 + Salle + Coworking (6 places)
    denom = CAPACITY_UNITS * open_days
    occupancy = round((booked_weight / denom) * 100, 1) if denom else 0.0

    cat_list = []
    for cat, cd in by_cat.items():
        cap = _CATEGORY_CAPACITY.get(cat, 1)
        cdenom = cap * open_days
        occ = round((cat_weight.get(cat, 0.0) / cdenom) * 100, 1) if cdenom else 0.0
        cat_list.append({"category": cat, "ca": round(cd["ca"], 2), "ca_ht": round(cd["ca_ht"], 2),
                         "count": cd["count"], "occupancy_pct": occ})
    cat_list.sort(key=lambda x: -x["ca"])
    offer_list = [{"offer": o["offer"], "ca": round(o["ca"], 2), "ca_ht": round(o["ca_ht"], 2),
                   "count": o["count"]} for o in by_offer.values()]
    offer_list.sort(key=lambda x: -x["ca"])
    top_clients = sorted(by_client.values(), key=lambda c: c["ca"], reverse=True)[:10]

    return {
        "total_ca": round(total_ca, 2),
        "count": count,
        "avg_basket": round((total_ca / count) if count else 0.0, 2),
        "occupancy_pct": occupancy,
        "by_space": [{"space": k, "ca": round(v, 2)} for k, v in sorted(by_space.items(), key=lambda x: -x[1])],
        "by_month": [{"month": k, "ca": round(v, 2)} for k, v in sorted(by_month.items())],
        "by_payment": [{"method": k, "ca": round(v, 2)} for k, v in sorted(by_pay.items(), key=lambda x: -x[1])],
        "by_category": cat_list,
        "by_offer": offer_list,
        "top_clients": [{**c, "ca": round(c["ca"], 2)} for c in top_clients],
    }


@router.get("/admin/stats")
def admin_stats(
    authorization: Optional[str] = Header(None),
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    test_mode: bool = Query(False),
):
    """Agrégats pour le tableau de bord statistique + comparaison période précédente."""
    _check_admin(authorization)
    from datetime import date as _date, timedelta
    sb = _supabase()

    # Période précédente de même longueur, juste avant date_from
    prev_from_s = prev_to_s = None
    try:
        d0 = _date.fromisoformat(date_from); d1 = _date.fromisoformat(date_to)
        length = (d1 - d0).days
        prev_to = d0 - timedelta(days=1)
        prev_from = prev_to - timedelta(days=length)
        prev_from_s, prev_to_s = prev_from.isoformat(), prev_to.isoformat()
    except Exception:
        pass

    qfrom = prev_from_s or date_from
    res = sb.table("cw_reservations") \
        .select("date,space,space_unit,slot,amount_ttc,payment_method,status,devis_status,test_mode,email,name,hour_from,hour_to") \
        .gte("date", qfrom).lte("date", date_to).execute()
    allrows = [r for r in (res.data or [])
               if bool(r.get("test_mode")) == test_mode and _reservation_is_active(r)]

    cur_rows = [r for r in allrows if date_from <= (r.get("date") or "") <= date_to]
    cur = _aggregate_stats(cur_rows, _open_days(date_from, date_to))

    prev = None
    if prev_from_s:
        prev_rows = [r for r in allrows if prev_from_s <= (r.get("date") or "") <= prev_to_s]
        prev = _aggregate_stats(prev_rows, _open_days(prev_from_s, prev_to_s))

    # --- Revenu des forfaits achetés sur la période (encaissé à l'achat des crédits) ---
    def _pack_revenue(dfrom, dto):
        try:
            pr = sb.table("cw_packs").select("amount_ttc,purchased_at,created_at,test_mode").execute()
        except Exception:
            return 0.0, 0, {}
        tot, n = 0.0, 0
        by_m = {}
        for p in (pr.data or []):
            if bool(p.get("test_mode")) != test_mode:
                continue
            ts = (p.get("purchased_at") or p.get("created_at") or "")[:10]
            amt = float(p.get("amount_ttc") or 0)
            if amt > 0 and ts and dfrom <= ts <= dto:
                tot += amt; n += 1
                mk = ts[:7]
                by_m[mk] = round(by_m.get(mk, 0.0) + amt, 2)
        return round(tot, 2), n, by_m

    pack_ca, pack_n, pack_by_month = _pack_revenue(date_from, date_to)
    if pack_ca:
        cur["pack_ca"] = pack_ca
        cur["pack_count"] = pack_n
        cur["total_ca"] = round(cur["total_ca"] + pack_ca, 2)
        # Ventilation mensuelle (graphique « Évolution du chiffre d'affaires »)
        _bm = {d["month"]: d["ca"] for d in (cur.get("by_month") or [])}
        for _k, _v in pack_by_month.items():
            _bm[_k] = round(_bm.get(_k, 0.0) + _v, 2)
        cur["by_month"] = [{"month": k, "ca": round(v, 2)} for k, v in sorted(_bm.items())]
        cur["by_payment"] = (cur.get("by_payment") or []) + [{"method": "Forfait (achat)", "ca": pack_ca}]
        cur["by_payment"].sort(key=lambda x: -x["ca"])
        cur["by_category"] = (cur.get("by_category") or []) + [{"category": "Forfait", "ca": pack_ca, "ca_ht": round(pack_ca / 1.20, 2), "count": pack_n, "occupancy_pct": 0.0}]
        cur["by_category"].sort(key=lambda x: -x["ca"])
        cur["by_offer"] = (cur.get("by_offer") or []) + [{"offer": "Forfait — achat de crédits", "ca": pack_ca, "ca_ht": round(pack_ca / 1.20, 2), "count": pack_n}]
        cur["by_offer"].sort(key=lambda x: -x["ca"])
    if prev:
        prev_pack_ca, _, _ = _pack_revenue(prev_from_s, prev_to_s)
        prev["total_ca"] = round(prev["total_ca"] + prev_pack_ca, 2)

    def _delta(curv, prevv):
        if prev is None or not prevv:
            return None
        return round((curv - prevv) / prevv * 100, 2)

    return {
        "period": {"from": date_from, "to": date_to},
        **cur,
        "previous": ({"from": prev_from_s, "to": prev_to_s,
                      "total_ca": prev["total_ca"], "occupancy_pct": prev["occupancy_pct"],
                      "count": prev["count"]} if prev else None),
        "delta_ca_pct": _delta(cur["total_ca"], prev["total_ca"]) if prev else None,
        "delta_occupancy_pct": _delta(cur["occupancy_pct"], prev["occupancy_pct"]) if prev else None,
        "delta_count_pct": _delta(cur["count"], prev["count"]) if prev else None,
    }


# ============================================================================
# Prospection — module unique (table cw_prospects)
# ============================================================================
PROSPECT_FIELDS = ("name", "activity", "location", "email", "phone", "channel",
                   "pitch", "disc", "profile_read", "status", "excluded",
                   "exclude_reason", "notes", "last_contact_at", "source", "date_added")
PROSPECT_STATUSES = ("a_contacter", "contacte", "relance", "client", "pas_interesse")


class ProspectIn(BaseModel):
    name: str
    activity: Optional[str] = None
    location: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    channel: Optional[str] = "email"
    pitch: Optional[str] = None
    disc: Optional[str] = None
    profile_read: Optional[str] = None
    source: Optional[str] = None


class ProspectUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    disc: Optional[str] = None
    profile_read: Optional[str] = None
    pitch: Optional[str] = None
    activity: Optional[str] = None
    location: Optional[str] = None
    channel: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    excluded: Optional[bool] = None
    exclude_reason: Optional[str] = None
    last_contact_at: Optional[str] = None


class ProspectIngest(BaseModel):
    prospects: List[ProspectIn]


@router.get("/admin/prospects")
def list_prospects(authorization: Optional[str] = Header(None),
                   status: Optional[str] = Query(None),
                   include_excluded: bool = Query(True)):
    """Liste tous les prospects (tous les jours confondus)."""
    _check_admin(authorization)
    sb = _supabase()
    q = sb.table("cw_prospects").select("*").order("created_at", desc=True)
    if status:
        q = q.eq("status", status)
    if not include_excluded:
        q = q.eq("excluded", False)
    res = q.execute()
    return {"prospects": res.data or [], "count": len(res.data or [])}


@router.post("/admin/prospects")
def create_prospect(payload: ProspectIn, authorization: Optional[str] = Header(None)):
    """Ajout manuel d'un prospect depuis la page admin."""
    _check_admin(authorization)
    if not payload.name:
        raise HTTPException(400, "Nom requis")
    sb = _supabase()
    row = {k: v for k, v in payload.dict().items() if v is not None}
    res = sb.table("cw_prospects").insert(row).execute()
    return {"ok": True, "prospect": res.data[0] if res.data else None}


@router.patch("/admin/prospects/{prospect_id}")
def update_prospect(prospect_id: int, payload: ProspectUpdate, authorization: Optional[str] = Header(None)):
    """Met à jour un prospect (statut, notes, DISC, exclusion…)."""
    _check_admin(authorization)
    if payload.status and payload.status not in PROSPECT_STATUSES:
        raise HTTPException(400, "Statut invalide")
    patch = {k: v for k, v in payload.dict().items() if v is not None}
    if not patch:
        return {"ok": True}
    sb = _supabase()
    sb.table("cw_prospects").update(patch).eq("id", prospect_id).execute()
    return {"ok": True}


@router.delete("/admin/prospects/{prospect_id}")
def delete_prospect(prospect_id: int, authorization: Optional[str] = Header(None)):
    """Supprime un prospect."""
    _check_admin(authorization)
    sb = _supabase()
    sb.table("cw_prospects").delete().eq("id", prospect_id).execute()
    return {"ok": True}


def _check_ingest_key(x_ingest_key: Optional[str]):
    if not COWORKING_PROSPECT_INGEST_KEY or x_ingest_key != COWORKING_PROSPECT_INGEST_KEY:
        raise HTTPException(401, "Clé d'ingestion invalide")


@router.get("/admin/prospects/known")
def prospects_known(x_ingest_key: Optional[str] = Header(None)):
    """Renvoie noms + emails déjà en base (pour déduplication par la tâche). Protégé par clé d'ingestion."""
    _check_ingest_key(x_ingest_key)
    sb = _supabase()
    res = sb.table("cw_prospects").select("name,email,excluded").execute()
    return {"known": [{"name": r.get("name"), "email": r.get("email"),
                       "excluded": r.get("excluded")} for r in (res.data or [])]}


@router.post("/admin/prospects/ingest")
def ingest_prospects(payload: ProspectIngest, x_ingest_key: Optional[str] = Header(None)):
    """Insertion en masse par la tâche de prospection quotidienne (dédup serveur).
    Ignore les prospects déjà connus (même email) et les noms exclus."""
    _check_ingest_key(x_ingest_key)
    sb = _supabase()
    existing = sb.table("cw_prospects").select("name,email,excluded").execute().data or []
    seen_emails = {(r.get("email") or "").strip().lower() for r in existing if r.get("email")}
    excluded_names = {(r.get("name") or "").strip().lower() for r in existing if r.get("excluded")}
    inserted, skipped = [], []
    for p in payload.prospects:
        nm = (p.name or "").strip()
        em = (p.email or "").strip().lower()
        if nm.lower() in excluded_names:
            skipped.append({"name": nm, "reason": "exclu"}); continue
        if em and em in seen_emails:
            skipped.append({"name": nm, "reason": "doublon"}); continue
        row = {k: v for k, v in p.dict().items() if v is not None}
        row["status"] = "a_contacter"
        sb.table("cw_prospects").insert(row).execute()
        if em:
            seen_emails.add(em)
        inserted.append(nm)
    return {"ok": True, "inserted": inserted, "skipped": skipped,
            "inserted_count": len(inserted), "skipped_count": len(skipped)}


@router.get("/admin/export/accounting")
def admin_export_accounting(
    authorization: Optional[str] = Header(None),
    date_from: str = Query(...),
    date_to: str = Query(...),
    test_mode: bool = Query(False),
):
    """Lignes comptables (HT/TVA/TTC) sur la période, pour export CSV côté front."""
    _check_admin(authorization)
    try:
        from coworking_invoices import analytic_code
    except Exception:
        def analytic_code(space="", source_type=""):
            return ""
    sb = _supabase()
    res = sb.table("cw_reservations") \
        .select("date,reference,space,space_unit,slot,amount_ttc,email,name,client_type,company,payment_method,status,devis_status,test_mode,stripe_invoice_id") \
        .gte("date", date_from).lte("date", date_to).order("date").execute()
    out = []
    for r in (res.data or []):
        if bool(r.get("test_mode")) != test_mode or not _reservation_is_active(r):
            continue
        ttc = float(r.get("amount_ttc") or 0)
        ht = round(ttc / 1.20, 2)
        tva = round(ttc - ht, 2)
        out.append({
            "date": r.get("date"),
            "reference": r.get("reference"),
            "client": r.get("name"),
            "email": r.get("email"),
            "societe": r.get("company"),
            "type_client": r.get("client_type"),
            "espace": r.get("space"),
            "code_analytique": analytic_code(r.get("space")),
            "unite": r.get("space_unit"),
            "creneau": r.get("slot"),
            "montant_ht": ht,
            "tva_20": tva,
            "montant_ttc": round(ttc, 2),
            "paiement": _payment_label(r),
            "facture_stripe": r.get("stripe_invoice_id"),
        })

    # Forfaits / packs achetés sur la période (revenu reconnu à l'achat)
    try:
        pk = sb.table("cw_packs") \
            .select("purchased_at,reference,customer_email,space,amount_ttc,test_mode") \
            .gte("purchased_at", date_from) \
            .lte("purchased_at", date_to + "T23:59:59") \
            .order("purchased_at").execute()
        for p in (pk.data or []):
            if bool(p.get("test_mode")) != test_mode:
                continue
            ttc = float(p.get("amount_ttc") or 0)
            if ttc <= 0:
                continue
            ht = round(ttc / 1.20, 2)
            tva = round(ttc - ht, 2)
            out.append({
                "date": (p.get("purchased_at") or "")[:10],
                "reference": p.get("reference"),
                "client": p.get("customer_email"),
                "email": p.get("customer_email"),
                "societe": None,
                "type_client": None,
                "espace": p.get("space"),
                "code_analytique": analytic_code(p.get("space"), "pack"),
                "unite": None,
                "creneau": "forfait",
                "montant_ht": ht,
                "tva_20": tva,
                "montant_ttc": round(ttc, 2),
                "paiement": "Forfait (Stripe)",
                "facture_stripe": None,
            })
    except Exception as e:
        print(f"[EXPORT] forfaits non inclus (non bloquant) : {e}")

    out.sort(key=lambda r: (r.get("date") or ""))
    return {"rows": out, "count": len(out)}


@router.get("/admin/export/clients")
def admin_export_clients(authorization: Optional[str] = Header(None)):
    """Liste de tous les clients (cw_customers) pour export CSV côté front."""
    _check_admin(authorization)
    sb = _supabase()
    try:
        res = sb.table("cw_customers").select(
            "email,name,civility,first_name,last_name,phone,company,company_siret,client_type,password_hash,created_at,last_booking_at,admin_notes,admin_tags"
        ).order("name").execute()
    except Exception:
        # Colonnes CRM pas encore créées → fallback sans notes/tags
        res = sb.table("cw_customers").select(
            "email,name,civility,first_name,last_name,phone,company,company_siret,client_type,password_hash,created_at,last_booking_at"
        ).order("name").execute()
    out = []
    for c in (res.data or []):
        out.append({
            "civilite": c.get("civility"),
            "prenom": c.get("first_name"),
            "nom": c.get("last_name"),
            "nom_complet": c.get("name"),
            "email": c.get("email"),
            "telephone": c.get("phone"),
            "societe": c.get("company"),
            "siret": c.get("company_siret"),
            "type_client": c.get("client_type"),
            "compte_actif": "oui" if c.get("password_hash") else "non",
            "cree_le": (str(c.get("created_at"))[:10] if c.get("created_at") else None),
            "derniere_resa": (str(c.get("last_booking_at"))[:10] if c.get("last_booking_at") else None),
            "notes": c.get("admin_notes"),
            "tags": c.get("admin_tags"),
        })
    return {"rows": out, "count": len(out)}


class CustomerCRMUpdate(BaseModel):
    email: str
    notes: Optional[str] = None
    tags: Optional[str] = None


@router.post("/admin/customers/crm")
def admin_update_customer_crm(payload: CustomerCRMUpdate, authorization: Optional[str] = Header(None)):
    """Enregistre les notes internes et tags d'un client (CRM)."""
    _check_admin(authorization)
    if not payload.email:
        raise HTTPException(400, "Email requis")
    sb = _supabase()
    patch = {}
    if payload.notes is not None:
        patch["admin_notes"] = payload.notes
    if payload.tags is not None:
        patch["admin_tags"] = payload.tags
    if not patch:
        return {"ok": True}
    try:
        sb.table("cw_customers").update(patch).ilike("email", payload.email.strip().lower()).execute()
    except Exception as e:
        raise HTTPException(500, f"Colonnes CRM manquantes ? {e}")
    return {"ok": True}


# ============================================================================
# Réserver pour un client (admin) — création directe d'une réservation
# ============================================================================
class AdminReservationCreate(BaseModel):
    space: str                       # bureau-1 | bureau-2 | salle-reunion | coworking
    date: str                        # YYYY-MM-DD
    hour_from: str                   # "08:00"
    hour_to: str                     # "12:00"
    name: str
    email: str
    amount_ttc: float = 0
    client_type: str = "particulier"
    company: Optional[str] = None
    generate_pin: bool = True
    send_email: bool = True
    test_mode: bool = False
    iad_rate: Optional[bool] = None   # True = force tarif iad −50% (salle) ; None = auto si conseiller validé


@router.post("/admin/reservations")
def admin_create_reservation(payload: AdminReservationCreate, authorization: Optional[str] = Header(None)):
    """Crée une réservation au nom d'un client (depuis le calendrier admin)."""
    _check_admin(authorization)
    conf = BOOKABLE_SPACES.get(payload.space)
    slug = conf["slug"] if conf else _norm_space_slug(payload.space)
    unit = conf["unit"] if conf else None
    space_label = conf["name"] if conf else payload.space
    hf = payload.hour_from or "08:00"
    ht = payload.hour_to or "18:00"
    if not payload.date:
        raise HTTPException(400, "Date requise")
    if _hm_to_minutes(ht) <= _hm_to_minutes(hf):
        raise HTTPException(400, "Plage horaire invalide")
    if not payload.email or "@" not in payload.email:
        raise HTTPException(400, "Email client requis")

    if hf == "08:00" and ht == "12:00":
        slot = "morning"
    elif hf == "14:00" and ht == "18:00":
        slot = "afternoon"
    elif hf == "08:00" and ht == "18:00":
        slot = "day"
    else:
        slot = "hour"

    # Tarif conseiller iad sur la SALLE DE RÉUNION : −50 % du tarif normal.
    # Une réunion d'équipe iad ne consomme PAS les demi-journées coworking : le
    # conseiller validé paie la salle moitié prix. Auto-appliqué si le client est
    # un conseiller iad validé ; forçable via iad_rate=True (ou désactivable =False).
    if slug == "salle-reunion" and payload.iad_rate is not False:
        apply_iad = payload.iad_rate
        if apply_iad is None:
            try:
                from coworking_iad import iad_is_validated
                apply_iad = iad_is_validated(payload.email) and not (payload.amount_ttc and payload.amount_ttc > 0)
            except Exception:
                apply_iad = False
        if apply_iad:
            try:
                from coworking_iad import iad_salle_amount_ttc
                auto = iad_salle_amount_ttc(slot, hf, ht)
                if auto is not None:
                    payload.amount_ttc = auto
            except Exception as e:
                print(f"[ADMIN-RESA] Tarif iad salle non appliqué : {e}")

    # Anti double-réservation (si espace connu)
    if conf:
        st = _slot_state(conf, payload.date, hf, ht, payload.test_mode)
        if not st.get("available"):
            raise HTTPException(409, st.get("reason") or "Créneau indisponible")

    reference = _generate_reference()
    start_dt, end_dt = _compute_datetimes(payload.date, slot, hf, ht)

    pin_code = pin_id = None
    if payload.generate_pin and IGLOOHOME_DEVICE_ID_COWORKING:
        try:
            from pole_sens import igloohome  # type: ignore
            access_name = f"{(payload.name or '')[:30]} {reference}"[:50]
            pin_start, pin_end = _pin_window(start_dt, end_dt)
            pin_data = igloohome.generate_custom_pin(
                device_id=IGLOOHOME_DEVICE_ID_COWORKING, start_date=pin_start, end_date=pin_end, name=access_name,
            )
            pin_code = pin_data.get("pin_code"); pin_id = pin_data.get("pin_id")
        except Exception as e:
            print(f"[ADMIN-RESA] Erreur PIN : {e}")

    sb = _supabase()
    row = {
        "reference": reference, "space": space_label, "space_unit": unit, "slot": slot,
        "date": payload.date, "hour_from": hf, "hour_to": ht,
        "amount_ttc": payload.amount_ttc or 0, "email": payload.email, "name": payload.name,
        "client_type": payload.client_type or "particulier", "company": payload.company,
        "pin_id": pin_id, "pin_code": pin_code, "test_mode": payload.test_mode,
        "status": "confirmed", "payment_method": "admin",
    }
    try:
        sb.table("cw_reservations").insert(row).execute()
        sb.table("cw_customers").upsert(
            {"email": payload.email, "name": payload.name, "company": payload.company},
            on_conflict="email",
        ).execute()
    except Exception as e:
        print(f"[ADMIN-RESA] Erreur insertion : {e}")
        raise HTTPException(500, "Erreur lors de l'enregistrement de la réservation")

    confirm_html = None
    if (payload.send_email and payload.email) or not payload.test_mode:
        confirm_html = _build_confirmation_email_html(
            client_name=payload.name, reference=reference, space=space_label, slot=slot,
            date_str=payload.date, hour_from=hf, hour_to=ht, amount=payload.amount_ttc or 0,
            pin_code=pin_code, start_dt=start_dt, end_dt=end_dt, invoice_pdf_url=None,
            offered=not (payload.amount_ttc and payload.amount_ttc > 0),
        )

    if payload.send_email and payload.email and confirm_html:
        try:
            _send_coworking_email(payload.email, f"Confirmation de réservation — L'Atelier du Coworking — {reference}", confirm_html)
        except Exception as e:
            print(f"[ADMIN-RESA] Erreur email client : {e}")

    # Notification au gérant pour toute nouvelle réservation admin (hors mode test)
    if not payload.test_mode:
        try:
            notif = _build_admin_notif_html(
                "Nouvelle réservation (saisie admin)",
                "Une réservation vient d'être créée depuis le back-office.",
                [
                    ("Client", f"{payload.name} · {payload.email}"),
                    ("Espace", space_label),
                    ("Date", payload.date),
                    ("Horaire", f"{hf} → {ht}"),
                    ("Montant", f"{(payload.amount_ttc or 0):.2f}".replace(".", ",") + " € TTC"),
                    ("Référence", reference),
                ],
                "Ouvrir le calendrier", f"{COWORKING_APP_BASE_URL}/admin-calendar",
            )
            _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Nouvelle réservation (admin) — {reference}", notif)
        except Exception as e:
            print(f"[ADMIN-RESA] Erreur notif gérant : {e}")

    return {"ok": True, "reference": reference, "pin_code": pin_code, "space": space_label}


# ============================================================================
# Événements + inscriptions
# ============================================================================
class EventCreate(BaseModel):
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    date: str
    hour_from: Optional[str] = None
    hour_to: Optional[str] = None
    location: Optional[str] = "L'Atelier du Coworking"
    capacity: Optional[int] = None
    image_url: Optional[str] = None
    price_ttc: Optional[float] = 0
    active: bool = True
    # Public cible : 'all' (tous), 'coworking' (coworkers), 'iad' (conseillers iad)
    audience: Optional[str] = "coworking"


_EVENT_AUDIENCES = ("all", "coworking", "iad")


def _norm_audience(v: Optional[str]) -> str:
    v = (v or "").strip().lower()
    return v if v in _EVENT_AUDIENCES else "coworking"


def _visible_audiences_for(email: Optional[str]) -> set:
    """Ensemble des 'audience' visibles par un visiteur :
      - conseiller iad (@iadfrance.fr connecté) : événements 'all' + 'iad'
      - coworker / visiteur non connecté       : événements 'all' + 'coworking'
    Les événements iad restent donc invisibles pour les coworkers (et le public),
    et les événements coworking (ex. Matinale Network) invisibles pour les iad."""
    if email and email.strip().lower().endswith("@iadfrance.fr"):
        return {"all", "iad"}
    return {"all", "coworking"}


def _event_optional_email(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        from coworking_client_auth import _get_authenticated_customer
        return _get_authenticated_customer(authorization).get("email")
    except Exception:
        return None


@router.get("/events")
def list_events(authorization: Optional[str] = Header(None)):
    """Événements à venir (public). Si connecté, indique is_registered."""
    sb = _supabase()
    today = datetime.now().strftime("%Y-%m-%d")
    my_email = _event_optional_email(authorization)
    allowed = _visible_audiences_for(my_email)
    res = sb.table("cw_events").select("*").eq("active", True).gte("date", today).order("date").execute()
    events = [e for e in (res.data or []) if _norm_audience(e.get("audience")) in allowed]
    ids = [e["id"] for e in events]
    counts, mine = {}, set()
    if ids:
        regs = sb.table("cw_event_registrations").select("event_id,customer_email").in_("event_id", ids).execute()
        for r in regs.data or []:
            counts[r["event_id"]] = counts.get(r["event_id"], 0) + 1
            if my_email and r["customer_email"] == my_email:
                mine.add(r["event_id"])
    out = []
    for e in events:
        cap = e.get("capacity")
        cnt = counts.get(e["id"], 0)
        out.append({**e, "registered_count": cnt,
                    "spots_left": (None if cap is None else max(cap - cnt, 0)),
                    "is_registered": e["id"] in mine})
    return {"events": out}


_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
              "août", "septembre", "octobre", "novembre", "décembre"]


def _event_date_fr(date_str: str, hf: str = "", ht: str = "") -> str:
    try:
        from datetime import date as _d
        d = _d.fromisoformat(date_str)
        s = f"{_FR_DAYS[d.weekday()]} {d.day} {_FR_MONTHS[d.month - 1]} {d.year}"
        if hf and ht:
            s += f" · {hf} → {ht}"
        return s
    except Exception:
        return date_str or ""


def _build_event_confirmation_html(name: str, ev: dict) -> str:
    prenom = _first_name(name)
    when = _event_date_fr(ev.get("date"), ev.get("hour_from"), ev.get("hour_to"))
    lieu = ev.get("location") or "20 rue Pasteur · 89100 Sens"
    rows = [("Événement", ev.get("title") or "—"), ("Date", when), ("Lieu", lieu)]
    trs = ""
    for i, (k, v) in enumerate(rows):
        top = "border-top:1px solid #F0EBE0" if i else ""
        trs += (f'<tr><td style="padding:9px 0;font-size:13.5px;color:#5A6A85;width:120px;{top}">{k}</td>'
                f'<td style="padding:9px 0;font-size:13.5px;color:#03234D;font-weight:600;{top}">{v}</td></tr>')
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#E9E7E1;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#E9E7E1;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#fff;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:28px;text-align:center">
<img src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png" alt="ACW" width="90" style="display:block;margin:0 auto 10px;border:0">
<p style="margin:0;font-family:Georgia,serif;font-size:12px;color:#fff;letter-spacing:.22em;text-transform:uppercase">L'Atelier du Coworking</p>
<p style="margin:6px 0 0;font-size:10.5px;color:#C9B584;letter-spacing:.24em;text-transform:uppercase">Inscription confirmée</p></td></tr>
<tr><td style="padding:32px 32px 12px">
<p style="margin:0 0 14px;font-size:15px;line-height:1.7">Bonjour <strong>{prenom}</strong>,</p>
<p style="margin:0 0 8px;font-size:14px;line-height:1.7">Votre inscription est confirmée. Nous serons ravis de vous y retrouver.</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:18px 0;border-top:1px solid #E5DDCB;border-bottom:1px solid #E5DDCB">{trs}</table>
<table cellpadding="0" cellspacing="0" border="0" style="margin:24px auto 6px"><tr><td align="center" style="border-radius:6px;background:#03234D">
<a href="{COWORKING_APP_BASE_URL}/evenements" target="_blank" style="display:inline-block;padding:12px 30px;font-family:Arial,sans-serif;font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:#fff;text-decoration:none">Voir l'événement</a></td></tr></table>
</td></tr>
<tr><td style="background:#F8F7F4;padding:22px 28px;text-align:center;border-top:1px solid #E5DDCB">
<p style="margin:0;font-family:Georgia,serif;font-size:14px;color:#03234D">David Landry — Fondateur</p>
<p style="margin:4px 0 0;font-size:11px;color:#3D4861;letter-spacing:.18em;text-transform:uppercase">L'Atelier du Coworking</p>
<p style="margin:10px 0 0;font-size:12px;color:#3D4861;line-height:1.7">20 rue Pasteur · 89100 Sens · Yonne<br>
<a href="mailto:ateliercoworking89@gmail.com" style="color:#3D4861;text-decoration:none">ateliercoworking89@gmail.com</a> · 06 23 88 05 03</p>
</td></tr>
</table></td></tr></table></body></html>"""


@router.post("/events/{event_id}/register")
def register_event(event_id: int, authorization: Optional[str] = Header(None)):
    from coworking_client_auth import _get_authenticated_customer
    cust = _get_authenticated_customer(authorization)
    sb = _supabase()
    ev = sb.table("cw_events").select("*").eq("id", event_id).limit(1).execute()
    if not ev.data:
        raise HTTPException(404, "Événement introuvable")
    event = ev.data[0]
    if float(event.get("price_ttc") or 0) > 0:
        raise HTTPException(402, "Cet événement est payant : l'inscription se fait via le paiement en ligne.")
    cap = event.get("capacity")
    if cap is not None:
        cur = sb.table("cw_event_registrations").select("id").eq("event_id", event_id).execute()
        if len(cur.data or []) >= cap:
            raise HTTPException(409, "Cet événement est complet.")
    try:
        sb.table("cw_event_registrations").insert({
            "event_id": event_id, "customer_email": cust["email"], "name": cust.get("name"),
        }).execute()
    except Exception:
        raise HTTPException(409, "Vous êtes déjà inscrit à cet événement.")

    # Email de confirmation au coworker
    try:
        html = _build_event_confirmation_html(cust.get("name"), event)
        _send_coworking_email(cust["email"], f"Inscription confirmée — {event.get('title')}", html)
    except Exception as e:
        print(f"[EVENT] Erreur email inscription : {e}")
    # Notification interne au gérant
    try:
        notif = _build_admin_notif_html(
            "Nouvelle inscription événement",
            "Un coworker vient de s'inscrire à un événement.",
            [("Coworker", f"{cust.get('name')} · {cust['email']}"),
             ("Événement", event.get("title") or "—"),
             ("Date", _event_date_fr(event.get("date"), event.get("hour_from"), event.get("hour_to")))],
            "Voir les inscrits", f"{COWORKING_APP_BASE_URL}/admin-evenements",
        )
        _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Inscription événement — {event.get('title')}", notif)
    except Exception as e:
        print(f"[EVENT] Erreur notif admin : {e}")
    return {"ok": True}


@router.delete("/events/{event_id}/register")
def unregister_event(event_id: int, authorization: Optional[str] = Header(None)):
    from coworking_client_auth import _get_authenticated_customer
    cust = _get_authenticated_customer(authorization)
    sb = _supabase()
    ev = sb.table("cw_events").select("title,date,hour_from,hour_to").eq("id", event_id).limit(1).execute()
    sb.table("cw_event_registrations").delete().eq("event_id", event_id).eq("customer_email", cust["email"]).execute()
    event = ev.data[0] if ev.data else {}
    title = event.get("title") or f"#{event_id}"
    # Email de confirmation de désinscription au coworker
    try:
        prenom = _first_name(cust.get("name"))
        when = _event_date_fr(event.get("date"), event.get("hour_from"), event.get("hour_to"))
        html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#E9E7E1;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#E9E7E1;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#fff;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:28px;text-align:center">
<img src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png" alt="ACW" width="90" style="display:block;margin:0 auto 10px;border:0">
<p style="margin:0;font-family:Georgia,serif;font-size:12px;color:#fff;letter-spacing:.22em;text-transform:uppercase">L'Atelier du Coworking</p>
<p style="margin:6px 0 0;font-size:10.5px;color:#C9B584;letter-spacing:.24em;text-transform:uppercase">Inscription annulée</p></td></tr>
<tr><td style="padding:32px 32px 12px">
<p style="margin:0 0 14px;font-size:15px;line-height:1.7">Bonjour <strong>{prenom}</strong>,</p>
<p style="margin:0 0 8px;font-size:14px;line-height:1.7">Votre inscription à <strong>{title}</strong>{(' · ' + when) if when else ''} a bien été annulée.</p>
<p style="margin:0 0 8px;font-size:14px;line-height:1.7">Au plaisir de vous accueillir à une prochaine rencontre.</p>
<table cellpadding="0" cellspacing="0" border="0" style="margin:22px auto 6px"><tr><td align="center" style="border-radius:6px;background:#03234D">
<a href="{COWORKING_APP_BASE_URL}/evenements" target="_blank" style="display:inline-block;padding:12px 30px;font-family:Arial,sans-serif;font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:#fff;text-decoration:none">Voir les événements</a></td></tr></table>
</td></tr>
<tr><td style="background:#F8F7F4;padding:22px 28px;text-align:center;border-top:1px solid #E5DDCB">
<p style="margin:0;font-family:Georgia,serif;font-size:14px;color:#03234D">David Landry — Fondateur</p>
<p style="margin:4px 0 0;font-size:11px;color:#3D4861;letter-spacing:.18em;text-transform:uppercase">L'Atelier du Coworking</p>
<p style="margin:10px 0 0;font-size:12px;color:#3D4861;line-height:1.7">20 rue Pasteur · 89100 Sens · Yonne<br>
<a href="mailto:ateliercoworking89@gmail.com" style="color:#3D4861;text-decoration:none">ateliercoworking89@gmail.com</a> · 06 23 88 05 03</p>
</td></tr>
</table></td></tr></table></body></html>"""
        _send_coworking_email(cust["email"], f"Inscription annulée — {title}", html)
    except Exception as e:
        print(f"[EVENT] Erreur email désinscription client : {e}")
    # Notification interne au gérant
    try:
        notif = _build_admin_notif_html(
            "Désinscription événement",
            "Un coworker s'est désinscrit d'un événement.",
            [("Coworker", f"{cust.get('name')} · {cust['email']}"), ("Événement", title)],
            "Voir les inscrits", f"{COWORKING_APP_BASE_URL}/admin-evenements",
        )
        _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Désinscription événement — {title}", notif)
    except Exception as e:
        print(f"[EVENT] Erreur notif désinscription : {e}")
    return {"ok": True}


# --- Accès public aux événements (sans compte) : page partageable + QR ---
class PublicEventReg(BaseModel):
    name: str
    email: str


def _public_event_dict(e: dict, cnt: int) -> dict:
    cap = e.get("capacity")
    return {"id": e.get("id"), "title": e.get("title"), "category": e.get("category"),
            "date": e.get("date"), "hour_from": e.get("hour_from"), "hour_to": e.get("hour_to"),
            "location": e.get("location"), "description": e.get("description"),
            "capacity": cap, "registered_count": cnt,
            "price_ttc": float(e.get("price_ttc") or 0),
            "full": (cap is not None and cnt >= cap)}


@router.get("/events/public")
def public_events_list():
    """Liste publique des événements à venir (aucune authentification).
    Public = coworkers/visiteurs → 'all' + 'coworking'. Les événements iad restent internes."""
    sb = _supabase()
    today = datetime.now().strftime("%Y-%m-%d")
    res = sb.table("cw_events").select("*").eq("active", True).gte("date", today).order("date").execute()
    events = [e for e in (res.data or []) if _norm_audience(e.get("audience")) in ("all", "coworking")]
    ids = [e["id"] for e in events]
    counts = {}
    if ids:
        regs = sb.table("cw_event_registrations").select("event_id").in_("event_id", ids).execute()
        for r in regs.data or []:
            counts[r["event_id"]] = counts.get(r["event_id"], 0) + 1
    return {"events": [_public_event_dict(e, counts.get(e["id"], 0)) for e in events]}


@router.get("/events/{event_id}/public")
def public_event(event_id: int):
    """Détail public d'un événement (aucune authentification)."""
    sb = _supabase()
    ev = sb.table("cw_events").select("*").eq("id", event_id).limit(1).execute()
    if not ev.data or not ev.data[0].get("active"):
        raise HTTPException(404, "Événement introuvable")
    e = ev.data[0]
    cnt = len(sb.table("cw_event_registrations").select("id").eq("event_id", event_id).execute().data or [])
    return _public_event_dict(e, cnt)


@router.post("/events/{event_id}/register-public")
def register_event_public(event_id: int, payload: PublicEventReg):
    """Inscription publique à un événement (nom + email, sans compte)."""
    sb = _supabase()
    email = (payload.email or "").strip().lower()
    name = (payload.name or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Email invalide")
    if not name:
        raise HTTPException(400, "Nom requis")
    ev = sb.table("cw_events").select("*").eq("id", event_id).limit(1).execute()
    if not ev.data or not ev.data[0].get("active"):
        raise HTTPException(404, "Événement introuvable")
    event = ev.data[0]
    if float(event.get("price_ttc") or 0) > 0:
        raise HTTPException(402, "Cet événement est payant : l'inscription se fait via le paiement en ligne.")
    regs = sb.table("cw_event_registrations").select("id,customer_email").eq("event_id", event_id).execute().data or []
    if any((r.get("customer_email") or "").strip().lower() == email for r in regs):
        return {"ok": True, "already": True}
    cap = event.get("capacity")
    if cap is not None and len(regs) >= cap:
        raise HTTPException(409, "Cet événement est complet.")
    try:
        sb.table("cw_event_registrations").insert({"event_id": event_id, "customer_email": email, "name": name}).execute()
    except Exception:
        return {"ok": True, "already": True}
    try:
        html = _build_event_confirmation_html(name, event)
        _send_coworking_email(email, f"Inscription confirmée — {event.get('title')}", html)
    except Exception as e:
        print(f"[EVENT-PUBLIC] email : {e}")
    try:
        notif = _build_admin_notif_html(
            "Nouvelle inscription événement (lien public)",
            "Une personne s'est inscrite via le lien public d'un événement.",
            [("Participant", f"{name} · {email}"), ("Événement", event.get("title") or "—"),
             ("Date", _event_date_fr(event.get("date"), event.get("hour_from"), event.get("hour_to")))],
            "Voir les inscrits", f"{COWORKING_APP_BASE_URL}/admin-evenements")
        _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Inscription événement (public) — {event.get('title')}", notif)
    except Exception as e:
        print(f"[EVENT-PUBLIC] notif : {e}")
    return {"ok": True}


@router.get("/admin/events")
def admin_list_events(authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_events").select("*").order("date", desc=True).execute()
    events = res.data or []
    ids = [e["id"] for e in events]
    counts = {}
    if ids:
        regs = sb.table("cw_event_registrations").select("event_id").in_("event_id", ids).execute()
        for r in regs.data or []:
            counts[r["event_id"]] = counts.get(r["event_id"], 0) + 1
    return {"events": [{**e, "registered_count": counts.get(e["id"], 0)} for e in events]}


@router.post("/admin/events")
def admin_create_event(payload: EventCreate, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    if not payload.title or not payload.date:
        raise HTTPException(400, "Titre et date requis")
    sb = _supabase()
    data = payload.dict(); data["audience"] = _norm_audience(data.get("audience"))
    res = sb.table("cw_events").insert(data).execute()
    event = res.data[0] if res.data else None
    # Événement « mardi Réseau » : inscrit d'office les membres Réseau actifs.
    enrolled = 0
    if event:
        try:
            from coworking_packs import autoenroll_reseau_members_for_event
            enrolled = autoenroll_reseau_members_for_event(event["id"])
        except Exception as e:
            print(f"[EVENT] Auto-inscription Réseau : {e}")
    return {"ok": True, "event": event, "reseau_enrolled": enrolled}


@router.patch("/admin/events/{event_id}")
def admin_update_event(event_id: int, payload: EventCreate, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    sb = _supabase()
    data = payload.dict(); data["audience"] = _norm_audience(data.get("audience"))
    sb.table("cw_events").update(data).eq("id", event_id).execute()
    # Si l'événement (re)devient « mardi Réseau », rattrape les inscriptions membres.
    enrolled = 0
    try:
        from coworking_packs import autoenroll_reseau_members_for_event
        enrolled = autoenroll_reseau_members_for_event(event_id)
    except Exception as e:
        print(f"[EVENT] Auto-inscription Réseau (update) : {e}")
    return {"ok": True, "reseau_enrolled": enrolled}


@router.delete("/admin/events/{event_id}")
def admin_delete_event(event_id: int, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    sb = _supabase()
    sb.table("cw_events").delete().eq("id", event_id).execute()
    return {"ok": True}


@router.get("/admin/events/{event_id}/registrations")
def admin_event_registrations(event_id: int, authorization: Optional[str] = Header(None)):
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_event_registrations").select("*").eq("event_id", event_id).order("created_at").execute()
    return {"registrations": res.data or [], "count": len(res.data or [])}


@router.get("/admin/companies")
def admin_companies(authorization: Optional[str] = Header(None)):
    """Vue B2B : agrège clients + réservations par société (champ company)."""
    _check_admin(authorization)
    sb = _supabase()
    custs = (sb.table("cw_customers").select("email,name,company,company_siret,phone").execute().data) or []
    resas = (sb.table("cw_reservations").select("email,name,company,amount_ttc,status,devis_status,reference,date,space").execute().data) or []

    comp = {}

    def _get(name):
        k = (name or "").strip().lower()
        if not k:
            return None
        if k not in comp:
            comp[k] = {"company": (name or "").strip(), "siret": None, "phone": None,
                       "_clients": {}, "reservations": [], "ca": 0.0, "reservation_count": 0}
        return comp[k]

    for cu in custs:
        c = _get(cu.get("company"))
        if not c:
            continue
        if cu.get("company_siret") and not c["siret"]:
            c["siret"] = cu["company_siret"]
        if cu.get("phone") and not c["phone"]:
            c["phone"] = cu["phone"]
        em = cu.get("email")
        if em:
            c["_clients"][em] = {"email": em, "name": cu.get("name")}

    for r in resas:
        c = _get(r.get("company"))
        if not c:
            continue
        active = (r.get("status") == "confirmed") or (r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"))
        if not active:
            continue
        amt = float(r.get("amount_ttc") or 0)
        c["ca"] += amt
        c["reservation_count"] += 1
        c["reservations"].append({"reference": r.get("reference"), "date": r.get("date"),
                                  "space": r.get("space"), "amount_ttc": amt, "name": r.get("name"), "email": r.get("email")})
        em = r.get("email")
        if em and em not in c["_clients"]:
            c["_clients"][em] = {"email": em, "name": r.get("name")}

    out = []
    for c in comp.values():
        clients = list(c.pop("_clients").values())
        c["clients"] = clients
        c["client_count"] = len(clients)
        c["ca"] = round(c["ca"], 2)
        c["reservations"] = sorted(c["reservations"], key=lambda x: x.get("date") or "", reverse=True)[:50]
        out.append(c)
    out.sort(key=lambda x: x["ca"], reverse=True)
    return {"companies": out, "count": len(out)}


class PricingUpdate(BaseModel):
    unit_price_ht: Optional[float] = None
    active: Optional[bool] = None
    name: Optional[str] = None
    description: Optional[str] = None


@router.get("/admin/pricing")
def admin_list_pricing(authorization: Optional[str] = Header(None)):
    """Tous les tarifs (actifs + inactifs) pour l'édition admin."""
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_pricing").select("*").order("sort_order").execute()
    return {"pricing": res.data or []}


@router.patch("/admin/pricing/{pricing_id}")
def admin_update_pricing(pricing_id: str, payload: PricingUpdate, authorization: Optional[str] = Header(None)):
    """Met à jour un tarif (prix HT, actif, nom, description)."""
    _check_admin(authorization)
    patch = {k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None}
    if not patch:
        return {"ok": True, "message": "Rien à mettre à jour"}
    sb = _supabase()
    sb.table("cw_pricing").update(patch).eq("id", pricing_id).execute()
    return {"ok": True, "updated": list(patch.keys())}


def _intervals_overlap(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    """Deux créneaux horaires (HH:MM) se chevauchent ?"""
    af, at = _hm_to_minutes(a_from), _hm_to_minutes(a_to)
    bf, bt = _hm_to_minutes(b_from), _hm_to_minutes(b_to)
    return af < bt and bf < at


def _count_overlapping_reservations(space: str, date_str: str, hour_from: str, hour_to: str, test_mode: bool = False) -> int:
    """
    Compte les réservations actives qui chevauchent ce créneau pour cet espace.
    Les devis test ne comptent QUE contre d'autres devis test, et inversement.
    Ça permet de tester la capacité en mode TEST sans polluer la prod.
    """
    sb = _supabase()
    res = sb.table("cw_reservations") \
        .select("hour_from,hour_to,space,status,devis_status,test_mode") \
        .eq("date", date_str) \
        .eq("test_mode", test_mode) \
        .execute()
    count = 0
    for r in res.data or []:
        # Filtre statut
        active = (r.get("status") == "confirmed") or (r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"))
        if not active:
            continue
        # Filtre espace (normalisé)
        r_space = (r.get("space") or "").lower().strip()
        # Reuse de la normalisation
        if "coworking" in r_space or "open" in r_space: r_space = "coworking"
        elif "bureau" in r_space: r_space = "bureau"
        elif "salle" in r_space or "réunion" in r_space or "reunion" in r_space: r_space = "salle-reunion"
        elif "privatis" in r_space: r_space = "privatisation"
        if r_space != space:
            continue
        # Chevauchement horaire
        if _intervals_overlap(hour_from, hour_to, r.get("hour_from") or "08:00", r.get("hour_to") or "18:00"):
            count += 1
    return count


def _has_privatisation_conflict(date_str: str, hour_from: str, hour_to: str, test_mode: bool = False) -> bool:
    """Y a-t-il une privatisation active qui chevauche ce créneau ?"""
    return _count_overlapping_reservations("privatisation", date_str, hour_from, hour_to, test_mode) > 0


@router.get("/customers/by-email")
def get_customer_by_email(
    email: str = Query(..., min_length=3),
    authorization: Optional[str] = Header(None),
):
    """
    Récupère un client connu par son email (case-insensitive).
    Renvoie ses infos pour pré-remplir le form devis, ou 404 si inconnu.
    """
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_customers") \
        .select("email,name,phone,company,company_siret,company_address,client_type,client_token") \
        .ilike("email", email.strip()) \
        .limit(1) \
        .execute()
    if not res.data:
        raise HTTPException(404, "Client inconnu")
    return res.data[0]


@router.get("/pricing")
def list_pricing(
    space: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """
    Liste les tarifs configurés.
    Filtrable par espace (coworking, bureau, salle-reunion, privatisation, option).
    """
    _check_admin(authorization)
    sb = _supabase()
    q = sb.table("cw_pricing") \
        .select("*") \
        .eq("active", True) \
        .order("sort_order")
    if space:
        q = q.eq("space", space)
    res = q.execute()
    return {"pricing": res.data or [], "count": len(res.data or [])}


@router.get("/devis")
def list_devis(
    status: Optional[str] = Query(None),
    test_mode: Optional[bool] = Query(None, description="True = uniquement TEST, False = uniquement prod, None = tous"),
    limit: int = Query(50, le=200),
    authorization: Optional[str] = Header(None),
):
    _check_admin(authorization)
    sb = _supabase()
    q = sb.table("cw_reservations") \
        .select("*") \
        .not_.is_("devis_reference", "null") \
        .order("created_at", desc=True) \
        .limit(limit)
    if status:
        q = q.eq("devis_status", status)
    if test_mode is not None:
        q = q.eq("test_mode", test_mode)
    res = q.execute()
    return {"devis": res.data or [], "count": len(res.data or [])}


@router.get("/calendar")
def list_calendar_events(
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
    include_test: bool = Query(False),
    authorization: Optional[str] = Header(None),
):
    """
    Renvoie tous les événements du calendrier (réservations + devis + slots bloqués).
    Format FullCalendar : [{id, title, start, end, color, extendedProps}]
    """
    _check_admin(authorization)
    sb = _supabase()

    # Couleurs par espace
    COLORS = {
        "coworking": "#C9B584",       # or
        "bureau": "#2563EB",          # bleu
        "salle-reunion": "#7C3AED",   # violet
        "privatisation": "#EA584A",   # corail
        "option": "#94A3B8",          # gris
    }

    # Normalisation des noms d'espaces (legacy : webhook utilisait "Espace coworking", etc.)
    def _normalize_space(s):
        if not s: return "coworking"
        x = s.lower().strip()
        if "coworking" in x or "open" in x: return "coworking"
        if "bureau" in x: return "bureau"
        if "salle" in x or "réunion" in x or "reunion" in x: return "salle-reunion"
        if "privatis" in x: return "privatisation"
        return x  # déjà un slug

    events: list[dict] = []

    # 1) Réservations + devis confirmés/validés
    q = sb.table("cw_reservations") \
        .select("id,reference,devis_reference,date,hour_from,hour_to,space,space_unit,slot,name,email,status,devis_status,test_mode,amount_total_ttc,amount_ttc,payment_mode") \
        .gte("date", date_from) \
        .lte("date", date_to)
    if not include_test:
        q = q.eq("test_mode", False)
    res = q.execute()

    for r in res.data or []:
        # On affiche les devis validés (créneau bloqué) + résa confirmées Stripe
        keep = False
        if r.get("status") == "confirmed":
            keep = True
        elif r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"):
            keep = True
        if not keep:
            continue

        space = _normalize_space(r.get("space"))
        color = COLORS.get(space, "#5A6A85")
        ref = r.get("devis_reference") or r.get("reference") or "—"
        hf = r.get("hour_from") or "09:00"
        ht = r.get("hour_to") or "18:00"
        space_unit = r.get("space_unit")
        title = f"{r.get('name', '?')} · {space_unit or space}"
        if r.get("test_mode"):
            title = "🧪 " + title

        events.append({
            "id": f"resa-{r['id']}",
            "title": title,
            "start": f"{r['date']}T{hf}:00",
            "end": f"{r['date']}T{ht}:00",
            "color": color,
            "extendedProps": {
                "type": "reservation",
                "resa_id": str(r["id"]),
                "reference": ref,
                "client_name": r.get("name"),
                "client_email": r.get("email"),
                "space": space,
                "space_unit": space_unit,
                "status": r.get("status"),
                "devis_status": r.get("devis_status"),
                "amount_ttc": r.get("amount_total_ttc") or r.get("amount_ttc"),
                "payment_mode": r.get("payment_mode"),
                "payment_method": r.get("payment_method"),
                "test_mode": bool(r.get("test_mode")),
            },
        })

    # 2) Slots bloqués manuellement
    try:
        res2 = sb.table("cw_blocked_slots") \
            .select("id,date,hour_from,hour_to,space,reason") \
            .gte("date", date_from) \
            .lte("date", date_to) \
            .execute()
        for b in res2.data or []:
            # Ignore les blocages qui pointent vers un devis déjà affiché
            reason = b.get("reason") or ""
            if reason.startswith("devis:"):
                continue
            space = _normalize_space(b.get("space"))
            events.append({
                "id": f"block-{b['id']}",
                "title": f"🚫 {reason or 'Bloqué'}",
                "start": f"{b['date']}T{b.get('hour_from') or '08:00'}:00",
                "end": f"{b['date']}T{b.get('hour_to') or '18:00'}:00",
                "color": "#475569",
                "extendedProps": {
                    "type": "blocked",
                    "reason": reason,
                    "space": space,
                },
            })
    except Exception as e:
        print(f"[CALENDAR] cw_blocked_slots inaccessible : {e}")

    return {"events": events, "count": len(events)}


# ============================================================================
# Templates emails
# ============================================================================
def _email_shell(title: str, accent_label: str, body_inner: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px">
  <tr><td align="center">
    <table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">
      <tr><td style="background:#03234D;padding:28px 28px 24px;text-align:center">
        <img src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png" alt="ACW" width="100" height="100" style="display:block;margin:0 auto 10px;border:0;">
        <p style="margin:0;font-family:'Cormorant Garamond',Georgia,serif;font-size:22px;font-weight:600;color:#FFFFFF;letter-spacing:0.5px;">{title}</p>
        <p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:11px;color:#C9B584;letter-spacing:3px;text-transform:uppercase;">{accent_label}</p>
      </td></tr>
      <tr><td style="padding:32px 28px;">{body_inner}</td></tr>
      <tr><td style="background:#F8F7F4;padding:18px 28px;text-align:center;font-size:11px;color:#5A6A85;border-top:1px solid #E5DDCB;">
        L'Atelier du Coworking Sens · 20 rue Pasteur · 89100 Sens<br>
        <a href="https://coworking-sens.com" style="color:#C9B584;text-decoration:none;">coworking-sens.com</a>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _btn_pdf(label: str, url: str) -> str:
    return (f'<p style="margin:24px 0;text-align:center"><a href="{url}" '
            f'style="display:inline-block;background:#03234D;color:#FFFFFF;text-decoration:none;'
            f'padding:12px 28px;border-radius:4px;font-family:Arial,sans-serif;font-size:12px;'
            f'letter-spacing:0.1em;text-transform:uppercase;font-weight:600;">{label}</a></p>')


def _render_email_devis(devis: dict, pdf_url: str) -> str:
    prenom = _first_name(devis.get("name"))
    validity = str(devis.get("devis_validity_until", ""))[:10]
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Suite à votre demande, voici votre devis n° <b>{devis['devis_reference']}</b>
      pour un montant total de <b>{_format_money(devis.get('amount_total_ttc', 0))} TTC</b>.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Ce devis est valable jusqu'au <b>{validity}</b>.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      <b>Pour le valider</b>, répondez simplement à cet email ou appelez-nous au
      <a href="tel:{COWORKING_PHONE}" style="color:#03234D;">{COWORKING_PHONE}</a>.
      Nous vous enverrons ensuite une facture d'acompte (30 %) à régler par virement
      pour confirmer définitivement votre réservation.
    </p>
    {_btn_pdf("Voir mon devis", pdf_url)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      À très vite,<br><b>David</b> — L'Atelier du Coworking
    </p>
    """
    return _email_shell("L'Atelier du Coworking", f"Devis · {devis['devis_reference']}", body)


def _render_email_acompte_demande(devis: dict, pdf_url: str) -> str:
    prenom = _first_name(devis.get("name"))
    acompte = _format_money(devis.get("amount_acompte_ttc", 0))
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Merci pour votre validation. Votre créneau est désormais <b>bloqué</b> en votre nom.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Pour finaliser votre réservation, merci de régler l'acompte de
      <b>{acompte} TTC</b> par virement.
    </p>
    {_btn_pdf("Voir ma facture d'acompte", pdf_url)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      Dès réception, vous recevrez votre code d'accès au coworking par email.<br><br>
      <b>David</b> — L'Atelier du Coworking
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Facture d'acompte", body)


def _render_email_total_demande(devis: dict, pdf_url: str) -> str:
    prenom = _first_name(devis.get("name"))
    total = _format_money(devis.get("amount_total_ttc", 0))
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Merci pour votre validation. Votre créneau est désormais <b>bloqué</b> en votre nom.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Pour finaliser votre réservation, merci de régler le montant total de
      <b>{total} TTC</b> par virement.
    </p>
    {_btn_pdf("Voir ma facture", pdf_url)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      Dès réception, vous recevrez votre code d'accès au coworking par email.<br><br>
      <b>David</b> — L'Atelier du Coworking
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Facture", body)


def _render_email_facture_acquittee(devis: dict, pdf_url: str) -> str:
    prenom = _first_name(devis.get("name"))
    total = _format_money(devis.get("amount_total_ttc", 0))
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Nous avons bien reçu votre règlement de <b>{total} TTC</b> — un grand merci !
      Votre réservation est définitivement confirmée.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Vous trouverez ci-dessous votre <b>facture acquittée</b>.
    </p>
    {_btn_pdf("Voir ma facture acquittée", pdf_url)}
    {_portal_block(devis)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      À très vite à l'Atelier !<br><b>David</b>
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Facture acquittée", body)


def _portal_block(devis: dict) -> str:
    """Bloc HTML "Accéder à mon espace" — à inclure dans les emails de confirmation."""
    portal_url = _get_client_portal_url(devis.get("email", ""))
    if not portal_url:
        return ""
    return f"""
    <div style="margin:24px 0;text-align:center;padding:18px;background:#F8F7F4;border-radius:6px;border-left:3px solid #C9B584;">
      <p style="margin:0 0 8px;font-size:11px;color:#5A6A85;letter-spacing:2px;text-transform:uppercase;font-weight:600;">Votre espace personnel</p>
      <p style="margin:0 0 12px;font-size:13px;color:#03234D;">Accédez à toutes vos réservations, factures et codes d'accès.</p>
      <a href="{portal_url}" style="display:inline-block;background:#03234D;color:#FFFFFF;text-decoration:none;padding:10px 24px;border-radius:4px;font-size:12px;letter-spacing:0.1em;text-transform:uppercase;font-weight:600;">Ouvrir mon espace</a>
    </div>
    """


def _wifi_block() -> str:
    """Bloc HTML Wifi — à inclure dans les emails d'accès/confirmation."""
    return """
    <div style="margin:16px 0;padding:14px 18px;background:#F8F7F4;border-radius:6px;border-left:3px solid #C9B584;">
      <p style="margin:0;font-size:11px;color:#5A6A85;letter-spacing:2px;text-transform:uppercase;font-weight:600;">Wifi</p>
      <p style="margin:6px 0 0;font-size:14px;color:#03234D;line-height:1.6;">Réseau : <b>Coworkingsens</b><br>Mot de passe : <b style="font-family:'Courier New',monospace;background:#FFFFFF;padding:2px 6px;border-radius:3px;">Cowork2023@@</b></p>
    </div>
    """


def _access_instructions_block() -> str:
    """Bloc HTML consignes d'accès au bâtiment — à inclure avec le code d'accès."""
    return """
    <div style="margin:16px 0;padding:14px 18px;background:#F8F7F4;border-radius:6px;border-left:3px solid #C9B584;">
      <p style="margin:0;font-size:11px;color:#5A6A85;letter-spacing:2px;text-transform:uppercase;font-weight:600;">Accès au bâtiment</p>
      <p style="margin:6px 0 0;font-size:14px;color:#03234D;line-height:1.6;">
        Rendez-vous au <b>20 rue Pasteur, 89100 Sens</b>. Tapez votre code d'accès sur le clavier numérique de la porte, puis appuyez sur la touche <b>cadenas</b>. Patientez 2 secondes, la porte s'ouvre.
      </p>
    </div>
    """


def _render_email_confirmation_acompte(devis: dict, pin: Optional[str]) -> str:
    prenom = _first_name(devis.get("name"))
    solde = float(devis.get("amount_total_ttc", 0) or 0) - float(devis.get("amount_paid_ttc", 0) or 0)
    pin_block = ""
    code_phrase = ""
    if pin:
        code_phrase = " Voici votre code d'accès :"
        pin_block = f"""
        <div style="margin:24px 0;text-align:center;padding:20px;background:#F8F7F4;border:1px dashed #C9B584;border-radius:4px;">
          <p style="margin:0;font-size:9px;color:#5A6A85;letter-spacing:3px;text-transform:uppercase;">Code d'accès</p>
          <p style="margin:8px 0 0;font-family:'Courier New',monospace;font-size:32px;font-weight:bold;color:#03234D;letter-spacing:6px;">{pin}</p>
        </div>"""
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Acompte bien reçu — votre réservation est <b>confirmée</b>.{code_phrase}
    </p>
    {pin_block}
    {(_access_instructions_block() + _wifi_block()) if pin else ""}
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      <b>Solde à régler avant la prestation :</b> {_format_money(solde)} (par virement, même RIB).
    </p>
    {_portal_block(devis)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      À très vite à l'Atelier !<br><b>David</b>
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Réservation confirmée", body)


def _render_email_access(devis: dict, pin: Optional[str]) -> str:
    """Email d'accès envoyé dès la validation du devis (le paiement peut suivre)."""
    prenom = _first_name(devis.get("name"))
    total = float(devis.get("amount_total_ttc", 0) or 0)
    paid = float(devis.get("amount_paid_ttc", 0) or 0)
    reste = round(total - paid, 2)

    recap_parts = []
    if devis.get("space"):
        recap_parts.append(devis["space"])
    if devis.get("date"):
        recap_parts.append(devis["date"])
    if devis.get("hour_from") and devis.get("hour_to"):
        recap_parts.append(f"{devis['hour_from']}–{devis['hour_to']}")
    recap = " · ".join(recap_parts)
    recap_html = (f'<p style="margin:0 0 16px;font-size:13px;color:#5A6A85;">{recap}</p>' if recap else "")

    pin_block = ""
    if pin:
        pin_block = f"""
        <div style="margin:24px 0;text-align:center;padding:20px;background:#F8F7F4;border:1px dashed #C9B584;border-radius:4px;">
          <p style="margin:0;font-size:9px;color:#5A6A85;letter-spacing:3px;text-transform:uppercase;">Code d'accès</p>
          <p style="margin:8px 0 0;font-family:'Courier New',monospace;font-size:32px;font-weight:bold;color:#03234D;letter-spacing:6px;">{pin}</p>
        </div>"""

    reste_html = ""
    if reste > 0.01:
        reste_html = (
            f'<p style="margin:0 0 16px;font-size:14px;line-height:1.7;">'
            f'<b>Règlement :</b> il reste <b>{_format_money(reste)} TTC</b> à régler par virement '
            f'(coordonnées sur votre facture). Merci de le finaliser avant la prestation.</p>'
        )

    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 8px;font-size:14px;line-height:1.7;">
      Votre réservation à L'Atelier du Coworking est <b>confirmée</b>. Voici votre code d'accès :
    </p>
    {recap_html}
    {pin_block}
    {_access_instructions_block()}
    {_wifi_block()}
    {reste_html}
    {_portal_block(devis)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      À très vite à l'Atelier !<br><b>David</b>
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Vos accès", body)


def _render_email_confirmation_total(devis: dict, pin: Optional[str]) -> str:
    prenom = _first_name(devis.get("name"))
    pin_block = ""
    code_phrase = ""
    if pin:
        code_phrase = " Voici votre code d'accès :"
        pin_block = f"""
        <div style="margin:24px 0;text-align:center;padding:20px;background:#F8F7F4;border:1px dashed #C9B584;border-radius:4px;">
          <p style="margin:0;font-size:9px;color:#5A6A85;letter-spacing:3px;text-transform:uppercase;">Code d'accès</p>
          <p style="margin:8px 0 0;font-family:'Courier New',monospace;font-size:32px;font-weight:bold;color:#03234D;letter-spacing:6px;">{pin}</p>
        </div>"""
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Paiement total reçu — votre réservation est <b>confirmée</b>.{code_phrase}
    </p>
    {pin_block}
    {(_access_instructions_block() + _wifi_block()) if pin else ""}
    {_portal_block(devis)}
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      Merci pour votre confiance.<br><b>David</b> — L'Atelier du Coworking
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Réservation confirmée", body)


def _render_email_solde_recu(devis: dict, solde_ttc: float) -> str:
    prenom = _first_name(devis.get("name"))
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Nous avons bien reçu votre virement de solde de <b>{_format_money(solde_ttc)}</b>.
      Votre dossier est complètement réglé. Merci !
    </p>
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      <b>David</b> — L'Atelier du Coworking
    </p>
    """
    return _email_shell("L'Atelier du Coworking", "Solde reçu", body)


# ============================================================================
# OPTION B — Réservation autonome client (page /reserver)
# Endpoints PUBLICS (pas d'auth admin) : disponibilités + demande privatisation
# ============================================================================

# Mapping des "espaces" côté front (page /reserver) → (slug capacité, unité, capacité)
# Bureau 1 et Bureau 2 sont deux unités indépendantes (capacité 1 chacune).
BOOKABLE_SPACES = {
    "bureau-1":      {"slug": "bureau",        "unit": "Bureau 1", "capacity": 1, "name": "Bureau 1"},
    "bureau-2":      {"slug": "bureau",        "unit": "Bureau 2", "capacity": 1, "name": "Bureau 2"},
    "salle-reunion": {"slug": "salle-reunion", "unit": None,       "capacity": 1, "name": "Salle de réunion"},
    "coworking":     {"slug": "coworking",     "unit": None,       "capacity": 6, "name": "Espace coworking"},
}

# Créneaux standard proposés à la réservation autonome (cohérents avec le prototype)
_STD_SLOTS = [
    {"key": "morning",   "label": "Matinée",    "hour_from": "08:00", "hour_to": "12:00"},
    {"key": "afternoon", "label": "Après-midi", "hour_from": "14:00", "hour_to": "18:00"},
    {"key": "day",       "label": "Journée",    "hour_from": "08:00", "hour_to": "18:00"},
]


def _norm_space_slug(s: Optional[str]) -> str:
    """Normalise un libellé d'espace vers son slug capacité."""
    r = (s or "").lower().strip()
    if "coworking" in r or "open" in r:
        return "coworking"
    if "bureau" in r:
        return "bureau"
    if "salle" in r or "réunion" in r or "reunion" in r:
        return "salle-reunion"
    if "privatis" in r:
        return "privatisation"
    return r


def _active_overlapping_rows(slug: str, date_str: str, hour_from: str, hour_to: str, test_mode: bool) -> list:
    """Réservations actives chevauchant ce créneau pour cet espace (avec leur unité)."""
    sb = _supabase()
    res = sb.table("cw_reservations") \
        .select("hour_from,hour_to,space,space_unit,status,devis_status,test_mode") \
        .eq("date", date_str) \
        .eq("test_mode", test_mode) \
        .execute()
    out = []
    for r in res.data or []:
        active = (r.get("status") == "confirmed") or (r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"))
        if not active:
            continue
        if _norm_space_slug(r.get("space")) != slug:
            continue
        if _intervals_overlap(hour_from, hour_to, r.get("hour_from") or "08:00", r.get("hour_to") or "18:00"):
            out.append(r)
    return out


def _is_closed(slug: str, date_str: str, hour_from: str, hour_to: str) -> bool:
    """Une fermeture manuelle (cw_blocked_slots) couvre-t-elle ce créneau ?
    space NULL = tout l'atelier fermé. Ignore les blocages liés à un devis."""
    try:
        sb = _supabase()
        res = sb.table("cw_blocked_slots") \
            .select("date,hour_from,hour_to,space,reason") \
            .eq("date", date_str) \
            .execute()
        for b in res.data or []:
            if (b.get("reason") or "").startswith("devis:"):
                continue
            b_space = b.get("space")
            if b_space and _norm_space_slug(b_space) != slug:
                continue  # fermeture d'un autre espace
            if _intervals_overlap(hour_from, hour_to, b.get("hour_from") or "08:00", b.get("hour_to") or "18:00"):
                return True
    except Exception as e:
        print(f"[AVAILABILITY] cw_blocked_slots inaccessible : {e}")
    return False


def _closure_reason(slug: str, date_str: str, hour_from: str, hour_to: str):
    """Renvoie le motif de la fermeture qui couvre ce créneau (None si ouvert).
    space NULL = tout l'atelier fermé. Ignore les blocages liés à un devis."""
    try:
        sb = _supabase()
        res = sb.table("cw_blocked_slots") \
            .select("date,hour_from,hour_to,space,reason") \
            .eq("date", date_str) \
            .execute()
        for b in res.data or []:
            rzn = (b.get("reason") or "")
            if rzn.startswith("devis:"):
                continue
            b_space = b.get("space")
            if b_space and _norm_space_slug(b_space) != slug:
                continue  # fermeture d'un autre espace
            if _intervals_overlap(hour_from, hour_to, b.get("hour_from") or "08:00", b.get("hour_to") or "18:00"):
                return rzn or "Fermeture"
    except Exception as e:
        print(f"[AVAILABILITY] cw_blocked_slots inaccessible : {e}")
    return None


def _slot_state(conf: dict, date_str: str, hour_from: str, hour_to: str, test_mode: bool) -> dict:
    """Calcule l'état d'un créneau pour un espace/unité donné."""
    slug = conf["slug"]
    unit = conf["unit"]
    capacity = conf["capacity"]

    # 1) Privatisation prioritaire : bloque tout
    if slug != "privatisation" and _has_privatisation_conflict(date_str, hour_from, hour_to, test_mode):
        return {"available": False, "remaining": 0, "reason": "Atelier privatisé", "closure_reason": "Atelier privatisé"}

    # 2) Fermeture manuelle (avec motif remonté au client)
    cr = _closure_reason(slug, date_str, hour_from, hour_to)
    if cr is not None:
        return {"available": False, "remaining": 0, "reason": "Fermé", "closure_reason": cr}

    rows = _active_overlapping_rows(slug, date_str, hour_from, hour_to, test_mode)

    if slug == "bureau":
        # Raisonne par unité nommée : Bureau 1 / Bureau 2 sont 2 salles distinctes.
        # Plusieurs résas sur la MÊME unité = 1 seule unité occupée (set déduplique).
        # Les résas sans unité (legacy) sont comptées comme occupant un bureau ambigu.
        UNITS = ("Bureau 1", "Bureau 2")
        explicit = set()
        null_count = 0
        for r in rows:
            u = (r.get("space_unit") or "").strip()
            if u in UNITS:
                explicit.add(u)
            else:
                null_count += 1
        remaining_units = max(len(UNITS) - len(explicit) - null_count, 0)
        available = (unit not in explicit) and (remaining_units > 0)
        return {
            "available": available,
            "remaining": 1 if available else 0,
            "reason": None if available else "Complet",
        }

    # coworking / salle-reunion : capacité simple
    remaining = max(capacity - len(rows), 0)
    return {
        "available": remaining > 0,
        "remaining": remaining,
        "reason": None if remaining > 0 else "Complet",
    }


@router.get("/availability")
def get_availability(
    response: Response,
    date: str = Query(..., description="YYYY-MM-DD"),
    space: str = Query(..., description="bureau-1 | bureau-2 | salle-reunion | coworking"),
    hour_from: Optional[str] = Query(None),
    hour_to: Optional[str] = Query(None),
    test_mode: bool = Query(False),
):
    """Disponibilités temps réel pour la page /reserver (public)."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    conf = BOOKABLE_SPACES.get(space)
    if not conf:
        raise HTTPException(400, "Espace inconnu")
    # Garde-fou : pas de réservation dans le passé
    try:
        if date < datetime.now().strftime("%Y-%m-%d"):
            raise HTTPException(400, "Date passée")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Date invalide")

    # Mode "créneau horaire personnalisé"
    if hour_from and hour_to:
        if _hm_to_minutes(hour_to) <= _hm_to_minutes(hour_from):
            raise HTTPException(400, "Plage horaire invalide")
        st = _slot_state(conf, date, hour_from, hour_to, test_mode)
        return {"date": date, "space": space, "name": conf["name"], "custom": {
            "hour_from": hour_from, "hour_to": hour_to, **st,
        }}

    # Mode "créneaux standard"
    slots = []
    for s in _STD_SLOTS:
        st = _slot_state(conf, date, s["hour_from"], s["hour_to"], test_mode)
        slots.append({**s, **st})
    return {"date": date, "space": space, "name": conf["name"], "slots": slots}


@router.post("/validate-slot")
def validate_slot(payload: dict = Body(...)):
    """Re-vérifie la dispo juste avant le paiement (anti double-réservation).
    Body: { space, date, hour_from, hour_to, test_mode? }"""
    space = payload.get("space")
    date = payload.get("date")
    hf = payload.get("hour_from") or "08:00"
    ht = payload.get("hour_to") or "18:00"
    test_mode = bool(payload.get("test_mode"))
    conf = BOOKABLE_SPACES.get(space)
    if not conf:
        raise HTTPException(400, "Espace inconnu")
    if not date or date < datetime.now().strftime("%Y-%m-%d"):
        raise HTTPException(400, "Date invalide")
    st = _slot_state(conf, date, hf, ht, test_mode)
    return {"ok": bool(st["available"]), "unit": conf["unit"], "slug": conf["slug"], **st}


class PrivatisationRequest(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    company: Optional[str] = None
    date: Optional[str] = None
    people: Optional[str] = None
    message: Optional[str] = None


@router.post("/privatisation-request")
def privatisation_request(payload: PrivatisationRequest):
    """Demande de devis privatisation depuis /reserver → email admin + accusé client."""
    if not payload.email or "@" not in payload.email:
        raise HTTPException(400, "Email invalide")
    prenom = (payload.name or "").split(" ")[0] or "Client"

    # 1) Email à l'admin
    admin_rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#5A6A85;">{lbl}</td>'
        f'<td style="padding:4px 0;color:#03234D;"><b>{val or "—"}</b></td></tr>'
        for lbl, val in [
            ("Nom", payload.name), ("Email", payload.email), ("Téléphone", payload.phone),
            ("Société", payload.company), ("Date envisagée", payload.date),
            ("Nb personnes", payload.people),
        ]
    )
    admin_body = f"""
    <p style="margin:0 0 16px;font-size:15px;line-height:1.6;">Nouvelle <b>demande de privatisation</b> via le site :</p>
    <table style="font-size:14px;border-collapse:collapse;margin:0 0 16px;">{admin_rows}</table>
    <p style="margin:0 0 6px;color:#5A6A85;font-size:13px;">Projet :</p>
    <p style="margin:0;padding:12px;background:#F8F7F4;border-radius:6px;font-size:14px;line-height:1.6;color:#03234D;">{(payload.message or "—")}</p>
    """
    admin_html = _email_shell("L'Atelier du Coworking", "Demande privatisation", admin_body)
    try:
        _send_coworking_email(COWORKING_ADMIN_EMAIL or payload.email, "[ACW] Demande de privatisation", admin_html)
    except Exception as e:
        print(f"[PRIVATISATION] erreur email admin : {e}")

    # 2) Accusé de réception au client
    client_body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Nous avons bien reçu votre demande de <b>privatisation de l'atelier</b>.
      David vous recontacte sous 24 h avec un devis personnalisé selon votre besoin.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Pour toute précision, répondez simplement à cet email ou appelez le 06 23 88 05 03.
    </p>
    <p style="margin:24px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">
      <b>David</b> — L'Atelier du Coworking
    </p>
    """
    client_html = _email_shell("L'Atelier du Coworking", "Demande reçue", client_body)
    try:
        _send_coworking_email(payload.email, "Votre demande de privatisation — L'Atelier du Coworking", client_html)
    except Exception as e:
        print(f"[PRIVATISATION] erreur email client : {e}")

    return {"ok": True, "message": "Demande envoyée"}


# ============================================================================
# RAPPELS DE RÉSERVATION (email automatique, déclenché par cron)
# ============================================================================
_MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]
_JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _date_fr(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{_JOURS_FR[d.weekday()]} {d.day} {_MOIS_FR[d.month - 1]} {d.year}"
    except Exception:
        return date_str or ""


def _build_reminder_email_html(r: dict) -> str:
    prenom = _first_name(r.get("name"))
    date_fr = _date_fr(r.get("date") or "")
    hf, ht = r.get("hour_from") or "", r.get("hour_to") or ""
    horaire = f"{hf} → {ht}" if hf and ht else ""
    space = r.get("space") or ""
    unit = r.get("space_unit")
    space_disp = space + (f" · {unit}" if unit else "")
    pin = r.get("pin_code")
    pin_block = ""
    if pin:
        pin_block = (
            '<div style="margin:18px 0;padding:16px;background:#F8F7F4;border:1px dashed #C9B584;border-radius:6px;text-align:center;">'
            '<p style="margin:0 0 4px;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#5A6A85;font-weight:700;">Votre code d\'accès</p>'
            f'<p style="margin:0;font-size:28px;font-weight:800;letter-spacing:4px;color:#03234D;">{pin}</p></div>'
        )
    body = (
        f'<p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>'
        '<p style="margin:0 0 16px;font-size:14px;line-height:1.7;">Petit rappel : vous avez une réservation à venir à <b>L\'Atelier du Coworking</b>.</p>'
        '<table style="font-size:14px;border-collapse:collapse;margin:0 0 8px;">'
        f'<tr><td style="padding:4px 14px 4px 0;color:#5A6A85;">Date</td><td style="padding:4px 0;color:#03234D;"><b>{date_fr}</b></td></tr>'
        + (f'<tr><td style="padding:4px 14px 4px 0;color:#5A6A85;">Horaire</td><td style="padding:4px 0;color:#03234D;"><b>{horaire}</b></td></tr>' if horaire else '')
        + f'<tr><td style="padding:4px 14px 4px 0;color:#5A6A85;">Espace</td><td style="padding:4px 0;color:#03234D;"><b>{space_disp}</b></td></tr>'
        f'<tr><td style="padding:4px 14px 4px 0;color:#5A6A85;">Référence</td><td style="padding:4px 0;color:#03234D;"><b>{r.get("reference", "—")}</b></td></tr>'
        '</table>'
        + pin_block
        + ((_access_instructions_block() + _wifi_block()) if pin else
           '<p style="margin:16px 0 0;font-size:14px;line-height:1.7;">Adresse : 20 rue Pasteur, 89100 Sens. À très vite !</p>')
        + '<p style="margin:16px 0 0;font-size:13px;color:#5A6A85;line-height:1.7;">Une question ? Répondez à cet email ou appelez le 06 23 88 05 03.<br><b>David</b> — L\'Atelier du Coworking</p>'
    )
    return _email_shell("Rappel de réservation", "Pensez-y", body)


@router.api_route("/cron/send-reminders", methods=["GET", "POST"])
def send_reservation_reminders(
    hours_before: int = Query(24, ge=1, le=72),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Envoie un email de rappel pour chaque réservation confirmée dont le début
    tombe dans la fenêtre [maintenant, maintenant + hours_before].
    Idempotent : pose reminder_sent_at pour ne jamais envoyer deux fois.
    Auth : header 'Authorization: Bearer <COWORKING_ADMIN_TOKEN>' ou '?token=<token>'.
    À déclencher par un cron (toutes les heures avec hours_before=14 → rappel ~12 h avant,
    ou une fois le matin avec hours_before=36 → rappel la veille)."""
    provided = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    provided = provided or token
    if not COWORKING_ADMIN_TOKEN or provided != COWORKING_ADMIN_TOKEN:
        raise HTTPException(401, "Non autorisé")

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Paris")
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.now()
    window_end = now + timedelta(hours=hours_before)

    sb = _supabase()
    res = sb.table("cw_reservations") \
        .select("id,reference,date,hour_from,hour_to,space,space_unit,slot,name,email,pin_code,status,devis_status,test_mode,reminder_sent_at") \
        .gte("date", now.strftime("%Y-%m-%d")) \
        .lte("date", window_end.strftime("%Y-%m-%d")) \
        .execute()

    sent, skipped = 0, 0
    for r in (res.data or []):
        if r.get("test_mode") or r.get("reminder_sent_at") or not r.get("email"):
            skipped += 1; continue
        ok_status = (r.get("status") == "confirmed") or (r.get("devis_status") in ("validated", "acompte_paid", "fully_paid"))
        if not ok_status:
            skipped += 1; continue
        hf = r.get("hour_from") or "09:00"
        try:
            start = datetime.strptime(f"{r['date']} {hf}", "%Y-%m-%d %H:%M")
            if tz:
                start = start.replace(tzinfo=tz)
        except Exception:
            skipped += 1; continue
        if not (now <= start <= window_end):
            skipped += 1; continue
        try:
            html = _build_reminder_email_html(r)
            _send_coworking_email(r["email"], "Rappel : votre réservation — L'Atelier du Coworking", html)
            sb.table("cw_reservations").update({"reminder_sent_at": now.isoformat()}).eq("id", r["id"]).execute()
            sent += 1
        except Exception as e:
            print(f"[REMINDER] erreur résa {r.get('id')}: {e}")
            skipped += 1

    return {"ok": True, "sent": sent, "skipped": skipped, "window_hours": hours_before, "now": now.isoformat()}


# ============================================================================
# Annulation / Remboursement / Avoirs (admin)
# ============================================================================
def _next_avoir_ref() -> str:
    """Génère la prochaine référence d'avoir : AV-<année>-NNNN (séquentiel)."""
    sb = _supabase()
    year = datetime.now().year
    prefix = f"AV-{year}-"
    try:
        res = sb.table("cw_avoirs").select("avoir_reference") \
            .like("avoir_reference", f"{prefix}%").execute()
        nums = []
        for row in (res.data or []):
            ref = row.get("avoir_reference") or ""
            tail = ref.replace(prefix, "")
            if tail.isdigit():
                nums.append(int(tail))
        nxt = (max(nums) + 1) if nums else 1
    except Exception as e:
        print(f"[AVOIR] erreur séquence : {e}")
        nxt = 1
    return f"{prefix}{nxt:04d}"


class CancelRefundRequest(BaseModel):
    reason: Optional[str] = ""
    refund_amount: Optional[float] = None      # None = remboursement total
    refund_method: Optional[str] = None        # 'stripe' | 'virement' | 'ticket' | 'none'
    notify_client: bool = True


def _stripe_refund_coworking(reservation: dict, amount_ttc: float) -> dict:
    """Déclenche un remboursement Stripe pour une réservation payée par carte.
    Retourne {ok, refund_id, error}."""
    session_id = reservation.get("stripe_session_id")
    if not session_id:
        return {"ok": False, "error": "stripe_session_id manquant sur la réservation"}
    is_test = bool(reservation.get("test_mode"))
    api_key = STRIPE_SECRET_KEY_COWORKING_TEST if is_test else STRIPE_SECRET_KEY_COWORKING
    if not api_key:
        return {"ok": False, "error": "Clé Stripe coworking non configurée"}
    try:
        import stripe  # type: ignore
        stripe.api_key = api_key
        session = stripe.checkout.Session.retrieve(session_id)
        payment_intent = session.get("payment_intent")
        if not payment_intent:
            return {"ok": False, "error": "payment_intent introuvable sur la session"}
        refund = stripe.Refund.create(
            payment_intent=payment_intent,
            amount=int(round(float(amount_ttc) * 100)),
        )
        return {"ok": True, "refund_id": refund.get("id")}
    except Exception as e:
        print(f"[REFUND] erreur Stripe : {e}")
        return {"ok": False, "error": str(e)}


@router.get("/admin/reservations/{reservation_id}/refund-estimate")
def reservation_refund_estimate(reservation_id: str, authorization: Optional[str] = Header(None)):
    """Montant remboursable en déduisant la commission Stripe.
    Lit la VRAIE commission via la balance transaction Stripe ; sinon estimation (1,5 % + 0,25 €)."""
    _check_admin(authorization)
    sb = _supabase()
    res = sb.table("cw_reservations").select("*").eq("id", reservation_id).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Réservation introuvable")
    r = res.data[0]
    amount = float(r.get("amount_ttc") or 0)
    fee = None
    source = "estimate"
    session_id = r.get("stripe_session_id")
    pay = (r.get("payment_method") or r.get("payment_mode") or "")
    if session_id and pay == "stripe":
        is_test = bool(r.get("test_mode"))
        api_key = STRIPE_SECRET_KEY_COWORKING_TEST if is_test else STRIPE_SECRET_KEY_COWORKING
        if api_key:
            try:
                import stripe  # type: ignore
                stripe.api_key = api_key
                session = stripe.checkout.Session.retrieve(
                    session_id, expand=["payment_intent.latest_charge.balance_transaction"])
                pi = session.get("payment_intent")
                charge = pi.get("latest_charge") if isinstance(pi, dict) else None
                bt = charge.get("balance_transaction") if isinstance(charge, dict) else None
                if bt and bt.get("fee") is not None:
                    fee = round(float(bt["fee"]) / 100.0, 2)
                    source = "stripe"
            except Exception as e:
                print(f"[REFUND-ESTIMATE] Stripe : {e}")
    if fee is None:
        fee = round(amount * 0.015 + 0.25, 2)
        source = "estimate"
    net = max(0.0, round(amount - fee, 2))
    return {"amount": round(amount, 2), "fee": fee, "net": net, "source": source}


def _build_avoir_email_html(reservation: dict, avoir_ref: str, amount_ttc: float,
                            refund_method: str, pdf_url: str, reason: str = "") -> str:
    prenom = _first_name(reservation.get("name"))
    date_str = str(reservation.get("date", ""))[:10]
    space = reservation.get("space") or "votre espace"
    if refund_method == "stripe":
        money_line = (f"Le montant de <b>{_format_money(amount_ttc)} TTC</b> vous est "
                      f"<b>remboursé sur la carte</b> ayant servi au paiement. Le délai d'apparition "
                      f"sur votre relevé est de 5 à 10 jours ouvrés selon votre banque.")
    elif refund_method == "ticket":
        money_line = ("Le <b>crédit (ticket)</b> utilisé pour cette réservation a été "
                      "<b>recrédité sur votre forfait</b>. Vous pouvez le réutiliser pour une prochaine réservation.")
    else:  # virement
        money_line = (f"Un avoir de <b>{_format_money(amount_ttc)} TTC</b> a été établi. "
                      f"Pour procéder au remboursement <b>par virement</b>, merci de nous communiquer "
                      f"votre IBAN en réponse à cet email.")
    btn = _btn_pdf("Télécharger mon avoir (PDF)", pdf_url) if refund_method != "ticket" else ""
    body = f"""
    <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">
      Votre réservation <b>{reservation.get('reference','')}</b> ({space}, le {date_str}) a bien été <b>annulée</b>.
    </p>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">{money_line}</p>
    {f'<p style="margin:0 0 16px;font-size:13px;line-height:1.6;color:#5A6A85;">Motif : {reason}</p>' if reason else ''}
    {f'<p style="margin:0 0 8px;font-size:14px;line-height:1.7;">Avoir n° <b>{avoir_ref}</b> :</p>{btn}' if refund_method != 'ticket' else ''}
    <p style="margin:24px 0 0;font-size:14px;line-height:1.7;">À très bientôt à l'Atelier,<br><strong>David</strong> — L'Atelier du Coworking</p>
    """
    return _email_shell("L'Atelier du Coworking", "Annulation & avoir", body)


@router.post("/admin/reservations/{reservation_id}/cancel")
def cancel_reservation_refund(reservation_id: str, payload: CancelRefundRequest,
                              authorization: Optional[str] = Header(None)):
    """Annule une réservation et gère le remboursement.
    - payée carte (stripe)   → remboursement Stripe (total ou partiel) + avoir PDF + email
    - payée forfait (pack)   → recrédite 1 ticket sur le forfait, pas d'avoir
    - autre / admin          → avoir « à rembourser par virement » + email demandant l'IBAN
    """
    _check_admin(authorization)
    res = _fetch_devis(reservation_id)
    if not res:
        raise HTTPException(404, "Réservation non trouvée")
    if (res.get("status") or "").lower() == "cancelled":
        raise HTTPException(400, "Cette réservation est déjà annulée")

    amount_paid = float(res.get("amount_ttc") or 0)
    pm_orig = (res.get("payment_method") or "").lower()

    # Méthode de remboursement : explicite, sinon déduite du mode de paiement d'origine
    method = (payload.refund_method or "").lower().strip()
    if not method:
        if pm_orig == "pack":
            method = "ticket"
        elif pm_orig == "stripe":
            method = "stripe"
        else:
            method = "virement"

    # Montant à rembourser (borné au montant payé)
    refund_amount = payload.refund_amount if payload.refund_amount is not None else amount_paid
    try:
        refund_amount = max(0.0, min(float(refund_amount), amount_paid))
    except Exception:
        refund_amount = amount_paid

    sb = _supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    avoir_ref = None
    refund_status = None
    result_extra = {}

    # --- 1) Recrédit ticket (forfait) ------------------------------------
    if method == "ticket":
        pack_id = res.get("pack_id")
        if pack_id:
            try:
                cur = sb.table("cw_packs").select("used_credits").eq("id", pack_id).limit(1).execute()
                if cur.data:
                    used = int(cur.data[0].get("used_credits") or 0)
                    sb.table("cw_packs").update({"used_credits": max(0, used - 1)}).eq("id", pack_id).execute()
                    result_extra["ticket_recredited"] = True
            except Exception as e:
                print(f"[REFUND] erreur recrédit ticket : {e}")
        refund_status = "credited"
        refund_amount = 0.0

    # --- 2) Remboursement Stripe ----------------------------------------
    elif method == "stripe":
        if refund_amount > 0:
            r = _stripe_refund_coworking(res, refund_amount)
            if not r.get("ok"):
                raise HTTPException(400, f"Remboursement Stripe impossible : {r.get('error')}")
            result_extra["stripe_refund_id"] = r.get("refund_id")
        refund_status = "refunded"

    # --- 3) Virement (à rembourser manuellement) ------------------------
    else:
        method = "virement"
        refund_status = "pending_iban"

    # --- Avoir (sauf recrédit ticket) -----------------------------------
    if method != "ticket":
        avoir_ref = _next_avoir_ref()
        try:
            sb.table("cw_avoirs").insert({
                "avoir_reference": avoir_ref,
                "reservation_id": str(reservation_id),
                "reservation_reference": res.get("reference"),
                "invoice_reference": (res.get("reference") or "").replace("RES-", "FAC-") or None,
                "customer_email": res.get("email"),
                "customer_name": res.get("name"),
                "amount_ttc": refund_amount,
                "refund_method": method,
                "refund_status": refund_status,
                "reason": payload.reason or "",
                "test_mode": bool(res.get("test_mode")),
            }).execute()
        except Exception as e:
            print(f"[AVOIR] erreur insertion avoir : {e}")

    # --- Mise à jour de la réservation (libère le créneau) --------------
    try:
        _update_devis(reservation_id, {
            "status": "cancelled",
            "cancelled_at": now_iso,
            "cancel_reason": payload.reason or "",
            "avoir_reference": avoir_ref,
            "refund_method": method,
            "refund_amount": refund_amount,
            "refund_status": refund_status,
        })
    except Exception as e:
        print(f"[REFUND] erreur maj réservation : {e}")
        raise HTTPException(500, "Erreur lors de la mise à jour de la réservation")

    # --- Email client ---------------------------------------------------
    if payload.notify_client and res.get("email"):
        try:
            pdf_url = f"{COWORKING_APP_BASE_URL}/api/coworking/avoir/{avoir_ref}.pdf" if avoir_ref else ""
            html = _build_avoir_email_html(res, avoir_ref or "", refund_amount, method, pdf_url, payload.reason or "")
            subj = "Annulation de votre réservation — L'Atelier du Coworking"
            _send_coworking_email(res["email"], subj, html)
        except Exception as e:
            print(f"[REFUND] erreur email client : {e}")

    # Notification interne au gérant
    if not res.get("test_mode"):
        try:
            method_label = {"stripe": "Remboursement carte", "ticket": "Recrédit forfait",
                            "virement": "Avoir (virement)"}.get(method, method)
            notif = _build_admin_notif_html(
                "Réservation annulée",
                "Une réservation a été annulée.",
                [
                    ("Client", f"{res.get('name')} · {res.get('email')}"),
                    ("Espace", res.get("space") or "—"),
                    ("Date", res.get("date") or "—"),
                    ("Montant remboursé", f"{refund_amount:.2f}".replace(".", ",") + " € TTC"),
                    ("Mode", method_label),
                    ("Motif", payload.reason or "—"),
                    ("Référence", res.get("reference") or "—"),
                ],
                "Ouvrir le calendrier", f"{COWORKING_APP_BASE_URL}/admin-calendar",
            )
            _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Réservation annulée — {res.get('reference') or ''}", notif)
        except Exception as e:
            print(f"[REFUND] erreur notif admin : {e}")

    return {
        "ok": True,
        "status": "cancelled",
        "refund_method": method,
        "refund_status": refund_status,
        "refund_amount": refund_amount,
        "avoir_reference": avoir_ref,
        **result_extra,
    }


@router.get("/avoir/{avoir_ref}.pdf")
def get_avoir_pdf(avoir_ref: str, authorization: Optional[str] = Header(None)):
    """Sert le PDF d'un avoir (note de crédit). Public via lien direct (référence unique)."""
    sb = _supabase()
    av = sb.table("cw_avoirs").select("*").eq("avoir_reference", avoir_ref).limit(1).execute()
    if not av.data:
        raise HTTPException(404, "Avoir non trouvé")
    avoir = av.data[0]
    reservation = {}
    rid = avoir.get("reservation_id")
    if rid:
        try:
            reservation = _fetch_devis(rid) or {}
        except Exception:
            reservation = {}
    # Fallback minimal si la réservation a disparu
    if not reservation:
        reservation = {
            "reference": avoir.get("reservation_reference"),
            "name": avoir.get("customer_name"),
            "email": avoir.get("customer_email"),
            "test_mode": avoir.get("test_mode"),
        }
    pdf = generate_coworking_avoir_pdf(
        reservation, avoir_ref, float(avoir.get("amount_ttc") or 0),
        reason=avoir.get("reason") or "", refund_method=avoir.get("refund_method") or "stripe",
    )
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="avoir-{avoir_ref}.pdf"'})


# ============================================================================
# Documents (factures + avoirs + devis) — vue admin unifiée
# ============================================================================
@router.get("/facture-resa/{reservation_id}.pdf")
def get_facture_resa_pdf(reservation_id: str):
    """PDF de facture d'une réservation (par id) — pour l'historique sans session Stripe.
    Public via lien direct, même logique que les PDF de devis/avoir."""
    r = _fetch_devis(reservation_id)
    if not r:
        raise HTTPException(404, "Réservation non trouvée")
    from webhook_coworking import generate_coworking_invoice_pdf
    pdf = generate_coworking_invoice_pdf(r, payment_method=(r.get("payment_method") or r.get("payment_mode") or "stripe"))
    ref = (r.get("reference") or "facture")
    fac = ref.replace("RES-", "FAC-") if ref.startswith("RES-") else f"FAC-{ref}"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fac}.pdf"'})


@router.get("/admin/documents")
def list_documents(authorization: Optional[str] = Header(None),
                   test_mode: Optional[bool] = Query(None),
                   limit: int = Query(800, le=2000)):
    """Vue unifiée : devis + factures + avoirs, avec lien PDF par ligne."""
    _check_admin(authorization)
    sb = _supabase()
    docs = []
    rq = sb.table("cw_reservations").select(
        "id,reference,devis_reference,devis_status,invoice_acompte_reference,invoice_solde_reference,"
        "date,name,email,company,status,amount_ttc,amount_total_ttc,amount_acompte_ttc,space,space_unit,slot,"
        "stripe_invoice_pdf_url,stripe_session_id,payment_method,payment_mode,test_mode,created_at"
    ).order("created_at", desc=True).limit(limit)
    if test_mode is not None:
        rq = rq.eq("test_mode", test_mode)
    for r in rq.execute().data or []:
        client = r.get("name") or "—"
        email = r.get("email")
        created = (r.get("created_at") or "")[:10]
        is_test = bool(r.get("test_mode"))
        esp = _category_label(r)
        resa_date = r.get("date")
        dref = r.get("devis_reference")
        if dref:
            docs.append({"type": "devis", "reference": dref, "date": created, "client": client, "email": email,
                         "amount": r.get("amount_total_ttc") or r.get("amount_ttc"),
                         "status": r.get("devis_status") or "—", "espace": esp, "resa_date": resa_date,
                         "pdf_url": f"/api/coworking/devis/{r['id']}.pdf", "test": is_test})
            if r.get("invoice_acompte_reference"):
                docs.append({"type": "facture", "reference": r["invoice_acompte_reference"], "date": created,
                             "client": client, "email": email, "amount": r.get("amount_acompte_ttc"),
                             "status": "acompte", "espace": esp, "resa_date": resa_date,
                             "pdf_url": f"/api/coworking/devis/{r['id']}.pdf", "test": is_test})
            if r.get("invoice_solde_reference"):
                docs.append({"type": "facture", "reference": r["invoice_solde_reference"], "date": created,
                             "client": client, "email": email, "amount": r.get("amount_total_ttc"),
                             "status": "solde", "espace": esp, "resa_date": resa_date,
                             "pdf_url": f"/api/coworking/devis/{r['id']}.pdf", "test": is_test})
        else:
            st = (r.get("status") or "").lower()
            if st in ("confirmed", "paid", "cancelled"):
                ref = r.get("reference") or ""
                facref = ref.replace("RES-", "FAC-") if ref.startswith("RES-") else (f"FAC-{ref}" if ref else "—")
                if r.get("stripe_invoice_pdf_url"):
                    pdf = r["stripe_invoice_pdf_url"]
                elif r.get("stripe_session_id"):
                    pdf = f"/api/coworking/invoice/{r['stripe_session_id']}.pdf"
                else:
                    pdf = f"/api/coworking/facture-resa/{r['id']}.pdf"
                docs.append({"type": "facture", "reference": facref, "date": created, "client": client, "email": email,
                             "amount": r.get("amount_ttc"),
                             "status": "annulée" if st == "cancelled" else "payée",
                             "espace": esp, "resa_date": resa_date,
                             "pdf_url": pdf, "test": is_test})
    for a in (sb.table("cw_avoirs").select("*").order("created_at", desc=True).limit(limit).execute().data or []):
        if test_mode is not None and bool(a.get("test_mode")) != test_mode:
            continue
        docs.append({"type": "avoir", "reference": a.get("avoir_reference"),
                     "date": (a.get("created_at") or "")[:10],
                     "client": a.get("customer_name") or "—", "email": a.get("customer_email"),
                     "amount": -abs(float(a.get("amount_ttc") or 0)),
                     "status": a.get("refund_status") or "—", "linked": a.get("invoice_reference"),
                     "pdf_url": f"/api/coworking/avoir/{a.get('avoir_reference')}.pdf",
                     "test": bool(a.get("test_mode"))})
    docs.sort(key=lambda d: (d.get("date") or ""), reverse=True)
    return {"documents": docs, "count": len(docs)}


class DocResend(BaseModel):
    reference: str
    email: str
    pdf_url: str
    client: Optional[str] = None
    doc_type: Optional[str] = "facture"


def _build_document_email_html(client_name: Optional[str], label: str, reference: str, url: str) -> str:
    greeting = f"Bonjour {client_name}," if client_name and client_name not in ("—", "") else "Bonjour,"
    article = "votre" if label != "avoir" else "votre"
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:{ACW_NAVY}">
  <div style="background:{ACW_NAVY};padding:24px;text-align:center;border-radius:10px 10px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:bold;letter-spacing:1.5px">L'ATELIER DU COWORKING</span>
  </div>
  <div style="background:#fff;border:1px solid {ACW_LIGHT_GREY};border-top:none;padding:28px;border-radius:0 0 10px 10px">
    <p>{greeting}</p>
    <p>Vous trouverez ci-dessous {article} {label} <strong>{reference}</strong>.</p>
    <p style="text-align:center;margin:28px 0">
      <a href="{url}" style="background:{ACW_GOLD};color:{ACW_NAVY};text-decoration:none;font-weight:bold;padding:14px 28px;border-radius:8px;display:inline-block">Voir / télécharger ma {label}</a>
    </p>
    <p style="font-size:13px;color:{ACW_SLATE}">Si le bouton ne fonctionne pas, copiez ce lien dans votre navigateur :<br><a href="{url}" style="color:{ACW_NAVY}">{url}</a></p>
    <p style="margin-top:24px">Pour toute question, écrivez-nous à <a href="mailto:{COWORKING_EMAIL}" style="color:{ACW_NAVY}">{COWORKING_EMAIL}</a>.</p>
    <p>Bien cordialement,<br><strong>{COWORKING_DISPLAY_NAME}</strong></p>
  </div>
  <p style="text-align:center;font-size:11px;color:{ACW_SLATE};margin-top:14px">{COWORKING_DISPLAY_NAME} · {COWORKING_PHONE} · {COWORKING_WEBSITE}</p>
</div>"""


@router.post("/admin/documents/resend")
def resend_document(payload: DocResend, authorization: Optional[str] = Header(None)):
    """Renvoie un document (facture / avoir / devis) au client par email, avec un lien vers le PDF."""
    _check_admin(authorization)
    email = (payload.email or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Adresse email du client manquante ou invalide.")
    pdf = (payload.pdf_url or "").strip()
    if not pdf:
        raise HTTPException(400, "Aucun PDF associé à ce document.")
    url = pdf if pdf.startswith("http") else f"{COWORKING_APP_BASE_URL.rstrip('/')}{pdf}"
    label = {"facture": "facture", "avoir": "avoir", "devis": "devis"}.get((payload.doc_type or "").lower(), "document")
    subject = f"Votre {label} {payload.reference} — {COWORKING_DISPLAY_NAME}"
    html = _build_document_email_html(payload.client, label, payload.reference, url)
    _send_coworking_email(email, subject, html)
    return {"ok": True, "sent_to": email}
