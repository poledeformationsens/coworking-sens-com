"""
Endpoint webhook Stripe pour le site de réservation coworking-sens.com

À ajouter à pole_sens.py (ou importer comme module).

Prérequis (env vars Render à ajouter) :
- STRIPE_WEBHOOK_SECRET_COWORKING (généré dans Stripe Dashboard → Webhooks)
- IGLOOHOME_DEVICE_ID_COWORKING (peut être le même que pole-iad-sens pour l'instant)
- COWORKING_EMAIL_FROM (par ex. reservation@coworking-sens.com avec alias Gmail)
"""

import io
import os
import smtplib
import stripe
import time
import httpx
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, HTTPException, Response
from typing import Optional

# === Imports reportlab pour génération PDF facture custom ===
from reportlab.lib import colors as rlcolors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
)


# ============================================================================
# Configuration
# ============================================================================

STRIPE_WEBHOOK_SECRET_COWORKING = os.getenv("STRIPE_WEBHOOK_SECRET_COWORKING", "")

# Clé Stripe API dédiée au coworking (TEST ou LIVE selon le mode)
# Doit correspondre au mode du webhook : si webhook = TEST, la clé doit être sk_test_*
STRIPE_SECRET_KEY_COWORKING = os.getenv("STRIPE_SECRET_KEY_COWORKING", "")

# La serrure utilisée pour le coworking (même que pole-iad-sens pour le moment)
IGLOOHOME_DEVICE_ID_COWORKING = os.getenv(
    "IGLOOHOME_DEVICE_ID_COWORKING",
    os.getenv("IGLOOHOME_DEVICE_ID", ""),  # fallback sur celle de pole-iad-sens
)

COWORKING_APP_BASE_URL = os.getenv("COWORKING_APP_BASE_URL", "https://coworking-sens.com")

# URL du backend FastAPI (pour générer le lien vers le PDF facture custom)
POLE_IAD_SENS_URL = os.getenv("POLE_IAD_SENS_URL", "https://pole-iad-sens.fr")

# === Infos entreprise (pour la facture custom) ===
COMPANY_DISPLAY_NAME = os.getenv("COMPANY_DISPLAY_NAME", "L'Atelier du Coworking")
COMPANY_LEGAL_NAME = os.getenv("COMPANY_LEGAL_NAME", "DL CONSULTING")
COMPANY_ADDRESS_LINE1 = os.getenv("COMPANY_ADDRESS_LINE1", "20 rue Pasteur")
COMPANY_ADDRESS_LINE2 = os.getenv("COMPANY_ADDRESS_LINE2", "89100 Sens")
COMPANY_PHONE = os.getenv("COMPANY_PHONE", "+33 6 23 88 05 03")
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "contact@coworking-sens.com")
COMPANY_SIRET = os.getenv("COMPANY_SIRET", "")  # ex: 88088657700019
COMPANY_VAT_NUMBER = os.getenv("COMPANY_VAT_NUMBER", "")  # ex: FR85880886577
COMPANY_WEBSITE = os.getenv("COMPANY_WEBSITE", "coworking-sens.com")

# RIB (utilisé pour les devis/privatisations à régler par virement)
COMPANY_BANK_NAME = os.getenv("COMPANY_BANK_NAME", "")
COMPANY_IBAN = os.getenv("COMPANY_IBAN", "")
COMPANY_BIC = os.getenv("COMPANY_BIC", "")

# === Configuration email — Resend HTTP API (recommandé) ===
# Clé dédiée au domaine coworking-sens.com (séparée de celle utilisée par pole-iad-sens.fr)
RESEND_API_KEY = os.getenv("RESEND_API_KEY_COWORKING", os.getenv("RESEND_API_KEY_CW", ""))
COWORKING_FROM_NAME = os.getenv("COWORKING_FROM_NAME", "L'Atelier du Coworking")
COWORKING_FROM_EMAIL = os.getenv("COWORKING_FROM_EMAIL", "reservation@coworking-sens.com")
COWORKING_REPLY_TO = os.getenv("COWORKING_REPLY_TO", "contact@coworking-sens.com")

# === Fallback Gmail SMTP (si Resend pas configuré) ===
COWORKING_GMAIL_USER = os.getenv("COWORKING_GMAIL_USER", "")
COWORKING_GMAIL_APP_PASSWORD = os.getenv("COWORKING_GMAIL_APP_PASSWORD", "").replace(" ", "")
FALLBACK_GMAIL_USER = os.getenv("GMAIL_FROM_EMAIL", "")
FALLBACK_GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")

# Labels lisibles pour les espaces (matche les valeurs envoyées par le front)
SPACE_LABELS = {
    "Bureau 1": "Bureau 1",
    "Bureau 2": "Bureau 2",
    "Salle de réunion": "Salle de réunion",
    "Espace coworking": "Espace coworking",
    "Privatisation atelier": "Privatisation de l'atelier",
}

SLOT_LABELS = {
    "morning": "Matinée (8h - 12h)",
    "afternoon": "Après-midi (14h - 18h)",
    "day": "Journée (8h - 18h)",
    "hour": "À l'heure",
}


# ============================================================================
# Router FastAPI
# ============================================================================

router = APIRouter(tags=["coworking"])


@router.post("/webhook/stripe-coworking")
async def stripe_webhook_coworking(request: Request):
    """
    Reçoit les événements Stripe pour le site coworking-sens.com.
    Configurer dans Stripe Dashboard → Développeurs → Webhooks :
      - URL    : https://pole-iad-sens.fr/webhook/stripe-coworking
      - Event  : checkout.session.completed
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET_COWORKING
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(status_code=400, detail=f"Webhook invalid: {e}")

    if event["type"] != "checkout.session.completed":
        # On ignore tous les autres événements pour l'instant
        return {"received": True, "skipped": event["type"]}

    session = event["data"]["object"]
    try:
        await _handle_coworking_payment(session)
    except Exception as e:
        # On log l'erreur mais on retourne 200 à Stripe pour éviter qu'il retry indéfiniment.
        # L'erreur peut être debugée via les logs Render + l'email admin envoyé.
        import traceback
        tb = traceback.format_exc()
        print(f"[COWORKING WEBHOOK ERROR] {e}\n{tb}")
        _notify_admin_error(session, str(e), tb)

    return {"received": True}


# ============================================================================
# Logique métier
# ============================================================================

async def _handle_coworking_payment(session: dict):
    """
    1. Extrait les metadata
    2. Génère le PIN Igloohome
    3. Enregistre en Supabase
    4. Envoie l'email de confirmation
    """
    metadata = session.get("metadata", {}) or {}
    customer_email = session.get("customer_email") or metadata.get("email", "")
    amount_total = (session.get("amount_total") or 0) / 100  # centimes → euros

    space = metadata.get("space", "")
    slot = metadata.get("slot", "")
    date_str = metadata.get("date", "")
    hour_from = metadata.get("hourFrom", "")
    hour_to = metadata.get("hourTo", "")
    client_type = metadata.get("client_type", "particulier")
    client_name = metadata.get("client_name", "")
    company = metadata.get("company", "")
    reference = metadata.get("reference", _generate_reference())

    # Calcule les dates start/end pour Igloohome et l'agenda
    start_dt, end_dt = _compute_datetimes(date_str, slot, hour_from, hour_to)

    # Génère le PIN sauf pour Privatisation (sur devis, pas de paiement immédiat)
    pin_code = None
    pin_id = None
    if space != "Privatisation atelier" and IGLOOHOME_DEVICE_ID_COWORKING:
        try:
            # Réutilise la classe IgloohomeClient existante de pole_sens.py
            from pole_sens import igloohome  # type: ignore
            access_name = f"{client_name[:30]} {reference}"[:50]
            pin_data = igloohome.generate_custom_pin(
                device_id=IGLOOHOME_DEVICE_ID_COWORKING,
                start_date=start_dt,
                end_date=end_dt,
                name=access_name,
            )
            pin_code = pin_data.get("pin_code")
            pin_id = pin_data.get("pin_id")
        except Exception as e:
            print(f"[COWORKING] Erreur génération PIN : {e}")
            # On continue sans PIN, le client recevra son email sans code et appellera le 06.

    # Génère la facture Stripe (PDF + envoi auto au client)
    stripe_invoice_id = None
    stripe_invoice_pdf_url = None
    if space != "Privatisation atelier":  # Privatisation = devis, pas facture auto
        try:
            invoice_data = _create_stripe_invoice(
                customer_email=customer_email,
                client_name=client_name,
                client_type=client_type,
                company=company,
                space=space,
                slot=slot,
                date_str=date_str,
                hour_from=hour_from,
                hour_to=hour_to,
                amount_ttc=amount_total,
                reference=reference,
                session=session,
            )
            if invoice_data:
                stripe_invoice_id = invoice_data.get("id")
                stripe_invoice_pdf_url = invoice_data.get("pdf_url")
        except Exception as e:
            print(f"[COWORKING] Erreur création facture Stripe : {e}")

    # Sauvegarde en Supabase
    try:
        from pole_sens import supabase  # type: ignore
        supabase.table("cw_reservations").insert({
            "reference": reference,
            "space": space,
            "slot": slot,
            "date": date_str,
            "hour_from": hour_from or None,
            "hour_to": hour_to or None,
            "amount_ttc": amount_total,
            "email": customer_email,
            "name": client_name,
            "client_type": client_type,
            "company": company or None,
            "stripe_session_id": session.get("id"),
            "stripe_invoice_id": stripe_invoice_id,
            "stripe_invoice_pdf_url": stripe_invoice_pdf_url,
            "pin_id": pin_id,
            "pin_code": pin_code,
            "status": "confirmed",
        }).execute()
        # Upsert client
        supabase.table("cw_customers").upsert({
            "email": customer_email,
            "name": client_name,
            "company": company or None,
            "last_booking_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="email").execute()
    except Exception as e:
        print(f"[COWORKING] Erreur Supabase : {e}")

    # URL vers le PDF de facture custom (généré on-the-fly par notre endpoint)
    # On expose coworking-sens.com/facture/... → Vercel proxy vers pole-iad-sens.fr
    # Comme ça aucune trace de pole-iad-sens.fr dans l'email du client
    custom_invoice_url = None
    sess_id = session.get("id")
    if sess_id and space != "Privatisation atelier":
        custom_invoice_url = f"{COWORKING_APP_BASE_URL}/facture/{sess_id}.pdf"

    # Envoie l'email de confirmation au client (PIN + lien facture en un seul email)
    try:
        html = _build_confirmation_email_html(
            client_name=client_name,
            reference=reference,
            space=space,
            slot=slot,
            date_str=date_str,
            hour_from=hour_from,
            hour_to=hour_to,
            amount=amount_total,
            pin_code=pin_code,
            start_dt=start_dt,
            end_dt=end_dt,
            invoice_pdf_url=custom_invoice_url,
        )
        subject = f"Confirmation de réservation — L'Atelier du Coworking — {reference}"
        _send_coworking_email(customer_email, subject, html)
        # Copie à l'admin pour suivi
        _send_coworking_email("david.landry@coworking-sens.com", f"[ACW] {subject}", html)
    except Exception as e:
        print(f"[COWORKING] Erreur envoi email : {e}")


# ============================================================================
# Helpers
# ============================================================================

def _generate_reference() -> str:
    """Génère une référence type RES-2026-XXXX."""
    import secrets
    year = datetime.now().year
    n = secrets.randbelow(9000) + 1000
    return f"RES-{year}-{n}"


def _create_stripe_invoice(
    *,
    customer_email: str,
    client_name: str,
    client_type: str,
    company: str,
    space: str,
    slot: str,
    date_str: str,
    hour_from: str,
    hour_to: str,
    amount_ttc: float,
    reference: str,
    session: dict,
) -> Optional[dict]:
    """
    Crée une facture Stripe à partir des infos du paiement.
    - Crée/récupère le Customer Stripe
    - Crée un Invoice item + un Invoice (auto-advance)
    - Marque la facture payée (puisque le paiement Stripe Checkout est déjà passé)
    - Stripe envoie le PDF par email au client si "send_invoice"
    Retourne {id, pdf_url, hosted_url} ou None en cas d'erreur.
    """
    # Clé API dédiée — évite de prendre celle globale de pole-iad-sens (sk_live au lieu de sk_test)
    api_key = STRIPE_SECRET_KEY_COWORKING or None

    # 1) Customer Stripe : recherche existant par email, sinon création
    try:
        existing = stripe.Customer.list(email=customer_email, limit=1, api_key=api_key)
        if existing.data:
            customer = existing.data[0]
        else:
            customer_params = {
                "email": customer_email,
                "name": company if (client_type == "pro" and company) else client_name,
                "metadata": {"reference": reference, "client_type": client_type},
                "api_key": api_key,
            }
            # Pour les pros, ajoute le nom du contact dans la description
            if client_type == "pro" and company:
                customer_params["description"] = f"Contact : {client_name}"
            customer = stripe.Customer.create(**customer_params)
    except Exception as e:
        print(f"[STRIPE INVOICE] Erreur Customer : {e}")
        return None

    # 2) Construit la description du line item
    slot_label = SLOT_LABELS.get(slot, slot)
    if slot == "hour" and hour_from and hour_to:
        slot_label = f"De {hour_from} à {hour_to}"
    date_fr = ""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_fr = _format_french_date(d)
    except Exception:
        date_fr = date_str

    description = f"{space} — {slot_label}"
    if date_fr:
        description += f" — {date_fr}"

    # 3) TVA 20% incluse — on calcule HT depuis TTC
    amount_ht = round(amount_ttc / 1.20, 2)
    amount_ht_cents = int(round(amount_ht * 100))

    # 4) Crée l'Invoice avec auto_advance=False pour pouvoir l'ajuster
    try:
        invoice = stripe.Invoice.create(
            customer=customer.id,
            collection_method="charge_automatically",  # pas de lien "Payer en ligne" — paiement déjà collecté
            description=f"Réservation L'Atelier du Coworking · {reference}",
            metadata={
                "reference": reference,
                "space": space,
                "slot": slot,
                "date": date_str,
                "session_id": session.get("id", ""),
            },
            default_tax_rates=[_get_or_create_tva_20(api_key=api_key)],
            auto_advance=False,
            footer="L'Atelier du Coworking Sens · 20 rue Pasteur · 89100 Sens\ncoworking-sens.com",
            api_key=api_key,
        )

        # 5) Ajoute le line item
        stripe.InvoiceItem.create(
            customer=customer.id,
            invoice=invoice.id,
            amount=amount_ht_cents,
            currency="eur",
            description=description,
            api_key=api_key,
        )

        # 6) Finalize la facture (génère le PDF)
        invoice = stripe.Invoice.finalize_invoice(invoice.id, api_key=api_key)

        # 7) Marque comme payée (sans frais Stripe additionnels — paid_out_of_band)
        invoice = stripe.Invoice.pay(invoice.id, paid_out_of_band=True, api_key=api_key)

        # 8) Attendre que Stripe régénère le PDF avec le statut "Payé" puis re-récupérer
        time.sleep(4)
        invoice = stripe.Invoice.retrieve(invoice.id, api_key=api_key)

        return {
            "id": invoice.id,
            "pdf_url": invoice.invoice_pdf,
            "hosted_url": invoice.hosted_invoice_url,
            "number": invoice.number,
        }
    except Exception as e:
        print(f"[STRIPE INVOICE] Erreur création/finalisation : {e}")
        return None


_TAX_RATE_CACHE = {}

def _get_or_create_tva_20(api_key: Optional[str] = None) -> str:
    """Retourne l'ID du tax_rate TVA 20% — créé une seule fois et caché (par mode TEST/LIVE)."""
    cache_key = f"tva_20_{api_key[:10] if api_key else 'default'}"
    if cache_key in _TAX_RATE_CACHE:
        return _TAX_RATE_CACHE[cache_key]
    try:
        # Cherche d'abord un tax rate existant
        rates = stripe.TaxRate.list(active=True, limit=100, api_key=api_key)
        for r in rates.data:
            if r.percentage == 20.0 and r.country == "FR" and r.inclusive is False:
                _TAX_RATE_CACHE[cache_key] = r.id
                return r.id
        # Sinon crée
        r = stripe.TaxRate.create(
            display_name="TVA",
            description="TVA 20% France",
            jurisdiction="FR",
            country="FR",
            percentage=20.0,
            inclusive=False,
            active=True,
            api_key=api_key,
        )
        _TAX_RATE_CACHE[cache_key] = r.id
        return r.id
    except Exception as e:
        print(f"[STRIPE INVOICE] Erreur TaxRate : {e}")
        return ""


def _compute_datetimes(date_str: str, slot: str, hour_from: str, hour_to: str):
    """Retourne (start_dt, end_dt) en UTC pour un créneau donné."""
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        base = datetime.now() + timedelta(days=1)

    if slot == "morning":
        start_h, end_h = 8, 12
    elif slot == "afternoon":
        start_h, end_h = 14, 18
    elif slot == "day":
        start_h, end_h = 8, 18
    elif slot == "hour" and hour_from and hour_to:
        start_h = int(hour_from.split(":")[0])
        end_h = int(hour_to.split(":")[0])
    else:
        start_h, end_h = 8, 18

    # Considère le fuseau Europe/Paris (UTC+1 ou +2 selon saison)
    # On simplifie : on stocke en UTC en soustrayant 1h (sera ajusté par Igloohome)
    start_dt = base.replace(hour=start_h, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end_dt = base.replace(hour=end_h, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return start_dt, end_dt


def _format_french_date(dt: datetime) -> str:
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt.weekday()]} {dt.day} {mois[dt.month - 1]} {dt.year}"


def _format_horaire(slot: str, hour_from: str, hour_to: str) -> str:
    if slot == "morning":
        return "8h00 → 12h00"
    if slot == "afternoon":
        return "14h00 → 18h00"
    if slot == "day":
        return "8h00 → 18h00"
    if slot == "hour" and hour_from and hour_to:
        return f"{hour_from} → {hour_to}"
    return ""


def _build_confirmation_email_html(
    *,
    client_name: str,
    reference: str,
    space: str,
    slot: str,
    date_str: str,
    hour_from: str,
    hour_to: str,
    amount: float,
    pin_code: Optional[str],
    start_dt: datetime,
    end_dt: datetime,
    invoice_pdf_url: Optional[str] = None,
) -> str:
    prenom = client_name.split(" ")[0] if client_name else "Bonjour"
    date_long = _format_french_date(start_dt)
    horaire = _format_horaire(slot, hour_from, hour_to)
    amount_str = f"{amount:.2f}".replace(".", ",") + " € TTC"

    # Bloc bouton "Télécharger ma facture"
    invoice_block = ""
    if invoice_pdf_url:
        invoice_block = f"""
<div style="background:#F8F7F4;border:1px solid #E5DDCB;border-radius:6px;padding:18px 22px;margin:24px 0;text-align:center;">
  <p style="margin:0 0 12px;font-family:Arial,sans-serif;font-size:11px;color:#C9B584;letter-spacing:2px;text-transform:uppercase;font-weight:600;">Votre facture</p>
  <p style="margin:0 0 14px;font-size:13px;color:#5A6A85;line-height:1.6;">
    Votre facture acquittée est disponible en téléchargement (PDF). Conservez-la pour votre comptabilité.
  </p>
  <a href="{invoice_pdf_url}" target="_blank" style="display:inline-block;background:#03234D;color:#FFFFFF;text-decoration:none;padding:10px 24px;border-radius:4px;font-family:Arial,sans-serif;font-size:12px;letter-spacing:0.1em;text-transform:uppercase;font-weight:600;">
    Télécharger ma facture
  </a>
</div>
"""

    pin_block = ""
    if pin_code:
        pin_block = f"""
<div style="background:#F8F7F4;border-left:4px solid #C9B584;padding:18px 22px;margin:24px 0;border-radius:4px">
  <p style="margin:0 0 10px;font-family:'Cormorant Garamond',Georgia,serif;font-size:13px;color:#C9B584;letter-spacing:2px;text-transform:uppercase;">Code d'accès</p>
  <p style="margin:0;font-family:'Courier New',monospace;font-size:32px;font-weight:bold;color:#03234D;letter-spacing:6px;">{pin_code}</p>
  <p style="margin:12px 0 0;font-size:13px;color:#5A6A85;line-height:1.6;">
    Une fois devant la porte au <strong>20 rue Pasteur, 89100 Sens</strong>, tapez ce code sur le clavier numérique, puis appuyez sur la touche <strong>🔓 cadenas</strong>. Patientez 2 secondes, la porte s'ouvrira.
  </p>
  <p style="margin:8px 0 0;font-size:12px;color:#888;font-style:italic;">
    Pour sortir, appuyez sur le bouton à l'intérieur de la serrure. Vérifiez que la porte se referme correctement.
  </p>
</div>
"""
    else:
        pin_block = """
<div style="background:#FFF7E0;border-left:4px solid #C9B584;padding:18px 22px;margin:24px 0;border-radius:4px">
  <p style="margin:0;font-size:14px;color:#03234D;line-height:1.6;">
    Votre code d'accès vous sera communiqué peu avant votre venue. Si vous ne l'avez pas reçu 1h avant votre réservation, contactez David au <strong>06 23 88 05 03</strong>.
  </p>
</div>
"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">

<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px">
  <tr><td align="center">
    <table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">

      <!-- En-tête navy -->
      <tr><td style="background:#03234D;padding:32px 28px;text-align:center">
        <p style="margin:0;font-family:'Cormorant Garamond',Georgia,serif;font-size:24px;font-weight:600;color:#FFFFFF;letter-spacing:0.5px;">L'Atelier du Coworking</p>
        <p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:11px;color:#C9B584;letter-spacing:3px;text-transform:uppercase;">Sens · 89 · Réservation confirmée</p>
      </td></tr>

      <!-- Corps -->
      <tr><td style="padding:32px 28px 8px;">
        <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">Bonjour <strong>{prenom}</strong>,</p>
        <p style="margin:0 0 16px;font-size:14px;line-height:1.7;">Votre réservation est confirmée. Voici les détails et votre code d'accès.</p>

        <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:24px 0;border-top:1px solid #E5DDCB;border-bottom:1px solid #E5DDCB;">
          <tr><td style="padding:12px 0;font-size:14px;color:#5A6A85;width:140px;">Référence</td>
              <td style="padding:12px 0;font-size:14px;color:#03234D;font-weight:600;">{reference}</td></tr>
          <tr><td style="padding:8px 0;font-size:14px;color:#5A6A85;border-top:1px solid #F0EBE0">Espace</td>
              <td style="padding:8px 0;font-size:14px;color:#03234D;font-weight:600;border-top:1px solid #F0EBE0">{space}</td></tr>
          <tr><td style="padding:8px 0;font-size:14px;color:#5A6A85;border-top:1px solid #F0EBE0">Date</td>
              <td style="padding:8px 0;font-size:14px;color:#03234D;border-top:1px solid #F0EBE0">{date_long}</td></tr>
          <tr><td style="padding:8px 0;font-size:14px;color:#5A6A85;border-top:1px solid #F0EBE0">Horaire</td>
              <td style="padding:8px 0;font-size:14px;color:#03234D;border-top:1px solid #F0EBE0">{horaire}</td></tr>
          <tr><td style="padding:8px 0 12px;font-size:14px;color:#5A6A85;border-top:1px solid #F0EBE0">Montant payé</td>
              <td style="padding:8px 0 12px;font-size:14px;color:#03234D;font-weight:600;border-top:1px solid #F0EBE0">{amount_str}</td></tr>
        </table>

        {pin_block}

        {invoice_block}

        <!-- Infos pratiques -->
        <p style="margin:24px 0 8px;font-family:'Cormorant Garamond',Georgia,serif;font-size:13px;color:#C9B584;letter-spacing:2px;text-transform:uppercase;">Informations pratiques</p>
        <table cellpadding="0" cellspacing="0" border="0" width="100%" style="font-size:13px;line-height:1.7;color:#03234D">
          <tr><td style="width:90px;color:#5A6A85;padding:4px 0">Adresse</td><td style="padding:4px 0">20 rue Pasteur · 89100 Sens</td></tr>
          <tr><td style="color:#5A6A85;padding:4px 0">Wifi</td><td style="padding:4px 0">Coworkingsens · <code style="background:#F8F7F4;padding:2px 6px;border-radius:3px;font-family:Courier,monospace">Cowork2023@@</code></td></tr>
          <tr><td style="color:#5A6A85;padding:4px 0">Contact</td><td style="padding:4px 0"><a href="tel:+33623880503" style="color:#03234D;text-decoration:none">06 23 88 05 03</a> · <a href="mailto:contact@coworking-sens.com" style="color:#03234D;text-decoration:none">contact@coworking-sens.com</a></td></tr>
        </table>

        <p style="margin:32px 0 8px;font-size:14px;color:#5A6A85;line-height:1.6;">Important :</p>
        <ul style="margin:0 0 24px;padding-left:20px;font-size:13px;color:#5A6A85;line-height:1.7">
          <li>Votre code est valable uniquement pendant la durée de votre réservation</li>
          <li>Merci de ne pas partager votre code d'accès</li>
          <li>Pour la sécurité de tous, ne laissez pas entrer de personnes non autorisées</li>
        </ul>

        <p style="margin:24px 0 0;font-size:14px;line-height:1.6">À très bientôt à l'atelier !</p>
        <p style="margin:8px 0 0;font-size:14px;line-height:1.6">David — L'Atelier du Coworking</p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="background:#F8F7F4;padding:18px 28px;text-align:center;border-top:1px solid #E5DDCB">
        <p style="margin:0;font-size:11px;color:#888;line-height:1.6">
          L'Atelier du Coworking Sens · 20 rue Pasteur · 89100 Sens<br>
          <a href="{COWORKING_APP_BASE_URL}" style="color:#C9B584;text-decoration:none">coworking-sens.com</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>

</body></html>"""


def _send_coworking_email(to_email: str, subject: str, html_body: str):
    """
    Envoi d'email coworking — Resend HTTP API en priorité (plus fiable sur Render free tier),
    fallback sur Gmail SMTP si Resend indisponible.
    From : "L'Atelier du Coworking <reservation@coworking-sens.com>"
    Reply-To : contact@coworking-sens.com
    """
    # === PRIORITÉ 1 : Resend HTTP API ===
    if RESEND_API_KEY:
        if _send_via_resend(to_email, subject, html_body):
            return
        print("[COWORKING EMAIL] Resend a échoué, tentative fallback SMTP…")

    # === PRIORITÉ 2 : Gmail SMTP (fallback) ===
    _send_via_smtp(to_email, subject, html_body)


def _send_via_resend(to_email: str, subject: str, html_body: str) -> bool:
    """Envoi via Resend HTTP API. Retourne True si succès."""
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": f"{COWORKING_FROM_NAME} <{COWORKING_FROM_EMAIL}>",
        "to": [to_email],
        "reply_to": COWORKING_REPLY_TO,
        "subject": subject,
        "html": html_body,
    }
    # Resend a une bonne tolérance aux hiccups, mais on retry 2 fois pour la robustesse
    for attempt in range(1, 3):
        try:
            with httpx.Client(timeout=15) as client:
                r = client.post(url, headers=headers, json=payload)
            if r.status_code in (200, 201):
                data = r.json()
                if attempt > 1:
                    print(f"[RESEND] ✓ envoi réussi à la tentative {attempt} pour {to_email} — id={data.get('id')}")
                return True
            print(f"[RESEND] tentative {attempt}/2 vers {to_email} — status={r.status_code} body={r.text[:300]}")
        except Exception as e:
            print(f"[RESEND] tentative {attempt}/2 vers {to_email} — erreur : {e}")
        if attempt < 2:
            time.sleep(2)
    print(f"[RESEND] ❌ ÉCHEC après 2 tentatives pour {to_email}")
    return False


def _send_via_smtp(to_email: str, subject: str, html_body: str):
    """Envoi via Gmail SMTP — fallback uniquement si Resend indispo."""
    if COWORKING_GMAIL_USER and COWORKING_GMAIL_APP_PASSWORD:
        smtp_user = COWORKING_GMAIL_USER
        smtp_password = COWORKING_GMAIL_APP_PASSWORD
        display_email = COWORKING_FROM_EMAIL
    elif FALLBACK_GMAIL_USER and FALLBACK_GMAIL_APP_PASSWORD:
        smtp_user = FALLBACK_GMAIL_USER
        smtp_password = FALLBACK_GMAIL_APP_PASSWORD
        display_email = FALLBACK_GMAIL_USER
        print("[COWORKING EMAIL] ⚠️ fallback SMTP pole-iad-sens (display adresse @pole-iad-sens.fr)")
    else:
        print("[COWORKING EMAIL] ❌ Aucune config email disponible (ni Resend ni SMTP)")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((COWORKING_FROM_NAME, display_email))
    msg["To"] = to_email
    msg["Reply-To"] = COWORKING_REPLY_TO
    msg.attach(MIMEText(html_body, "html"))

    last_error = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            if attempt > 1:
                print(f"[SMTP] ✓ envoi réussi à la tentative {attempt} pour {to_email}")
            return
        except Exception as e:
            last_error = e
            delay = attempt * 2
            print(f"[SMTP] tentative {attempt}/3 vers {to_email} échouée : {e} — retry dans {delay}s")
            if attempt < 3:
                time.sleep(delay)
    print(f"[SMTP] ❌ ÉCHEC DÉFINITIF vers {to_email} après 3 tentatives. Dernière erreur : {last_error}")


def _notify_admin_error(session: dict, error_msg: str, traceback_str: str):
    """Envoie un email à l'admin en cas d'erreur dans le traitement."""
    try:
        body = f"""<p>Erreur lors du traitement d'un paiement coworking-sens.com</p>
<p><strong>Session ID :</strong> {session.get('id')}</p>
<p><strong>Email client :</strong> {session.get('customer_email')}</p>
<p><strong>Montant :</strong> {(session.get('amount_total') or 0)/100} €</p>
<p><strong>Erreur :</strong> {error_msg}</p>
<pre style="background:#f4f4f4;padding:12px;font-size:11px">{traceback_str}</pre>"""
        _send_coworking_email("david.landry@coworking-sens.com",
                              "[ACW] ❌ Erreur webhook Stripe coworking",
                              body)
    except Exception:
        pass


# ============================================================================
# Génération de la facture PDF custom (reportlab) — flow Stripe
# ============================================================================

# Cache des logos / ressources externes
_LOGO_BYTES_CACHE: Optional[bytes] = None
_LOGO_URL = "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo.png"

# Couleurs ACW
ACW_NAVY = rlcolors.HexColor("#03234D")
ACW_GOLD = rlcolors.HexColor("#C9B584")
ACW_CREAM = rlcolors.HexColor("#F8F7F4")
ACW_SLATE = rlcolors.HexColor("#5A6A85")
ACW_LIGHT_GREY = rlcolors.HexColor("#E5DDCB")
ACW_GREEN = rlcolors.HexColor("#1D9E75")


def _get_logo_bytes() -> Optional[bytes]:
    """Télécharge et cache le logo ACW depuis le CDN."""
    global _LOGO_BYTES_CACHE
    if _LOGO_BYTES_CACHE is not None:
        return _LOGO_BYTES_CACHE or None
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(_LOGO_URL)
        if r.status_code == 200:
            _LOGO_BYTES_CACHE = r.content
            return _LOGO_BYTES_CACHE
    except Exception as e:
        print(f"[INVOICE PDF] Échec téléchargement logo : {e}")
    _LOGO_BYTES_CACHE = b""
    return None


def _format_money(amount: float) -> str:
    """Formate un montant en euros avec virgule décimale française."""
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _format_siret(siret: str) -> str:
    """Formate un SIRET '88088657700019' en '880 886 577 00019'."""
    s = (siret or "").replace(" ", "")
    if len(s) == 14:
        return f"{s[0:3]} {s[3:6]} {s[6:9]} {s[9:14]}"
    return siret


def generate_coworking_invoice_pdf(reservation: dict, payment_method: str = "stripe") -> bytes:
    """
    Génère le PDF de facture L'Atelier du Coworking en mémoire.
    `payment_method` :
      - "stripe" → mention "Payée par carte bancaire via Stripe"
      - "virement" → affiche le RIB pour règlement
    Retourne les bytes du PDF.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.8*cm,
        rightMargin=1.8*cm,
        topMargin=1.6*cm,
        bottomMargin=1.6*cm,
        title=f"Facture {reservation.get('reference', '')}",
        author=COMPANY_DISPLAY_NAME,
    )

    # === Styles ===
    style_title = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=28,
                                 textColor=ACW_NAVY, spaceAfter=0, leading=32)
    style_subtitle = ParagraphStyle("Sub", fontName="Helvetica", fontSize=9,
                                    textColor=ACW_GOLD, spaceAfter=14, leading=12,
                                    letterSpacing=2)
    style_h2 = ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=9,
                              textColor=ACW_GOLD, leading=12, spaceBefore=0, spaceAfter=4)
    style_body = ParagraphStyle("Body", fontName="Helvetica", fontSize=9.5,
                                textColor=ACW_NAVY, leading=14)
    style_body_strong = ParagraphStyle("BodyStrong", fontName="Helvetica-Bold", fontSize=10,
                                       textColor=ACW_NAVY, leading=14)
    style_small = ParagraphStyle("Small", fontName="Helvetica", fontSize=8,
                                 textColor=ACW_SLATE, leading=11)
    style_legal = ParagraphStyle("Legal", fontName="Helvetica", fontSize=7.5,
                                 textColor=ACW_SLATE, leading=10, alignment=TA_LEFT)
    style_status = ParagraphStyle("Status", fontName="Helvetica-Bold", fontSize=11,
                                  textColor=rlcolors.white, leading=14, alignment=TA_CENTER)
    style_footer = ParagraphStyle("Footer", fontName="Helvetica", fontSize=8,
                                  textColor=ACW_SLATE, leading=11, alignment=TA_CENTER)

    # === Préparation des données ===
    ref = reservation.get("reference", "")
    invoice_num = ref.replace("RES-", "FAC-") if ref.startswith("RES-") else f"FAC-{ref}"
    space = reservation.get("space", "")
    slot = reservation.get("slot", "")
    date_str = reservation.get("date", "")
    hour_from = reservation.get("hour_from", "") or ""
    hour_to = reservation.get("hour_to", "") or ""
    amount_ttc = float(reservation.get("amount_ttc", 0))
    amount_ht = round(amount_ttc / 1.20, 2)
    amount_tva = round(amount_ttc - amount_ht, 2)
    client_name = reservation.get("name", "")
    client_email = reservation.get("email", "")
    client_company = reservation.get("company") or ""
    client_type = reservation.get("client_type", "particulier")

    # Date d'émission = date de création de la résa
    created_at = reservation.get("created_at")
    if created_at:
        try:
            issued_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            issued_date = datetime.now(timezone.utc)
    else:
        issued_date = datetime.now(timezone.utc)
    issued_str = _format_french_date(issued_date)

    # Date de prestation (résa)
    try:
        booking_date = datetime.strptime(date_str, "%Y-%m-%d")
        booking_str = _format_french_date(booking_date)
    except Exception:
        booking_str = date_str

    # Libellé du créneau
    if slot == "hour" and hour_from and hour_to:
        slot_label = f"De {hour_from} à {hour_to}"
    else:
        slot_label = SLOT_LABELS.get(slot, slot)

    description_ligne = f"{space} — {slot_label} — {booking_str}"

    # === Construction des éléments ===
    elements = []

    # 1) Header : logo à droite + titre à gauche
    logo_bytes = _get_logo_bytes()
    title_block = [
        Paragraph("FACTURE", style_title),
        Paragraph("L'ATELIER DU COWORKING", style_subtitle),
    ]
    if logo_bytes:
        logo_img = Image(io.BytesIO(logo_bytes), width=3.2*cm, height=3.2*cm, kind="proportional")
        header_table = Table(
            [[title_block, logo_img]],
            colWidths=[12*cm, 5.4*cm]
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        elements.append(header_table)
    else:
        for el in title_block:
            elements.append(el)

    elements.append(Spacer(1, 6))

    # 2) Numéro de facture + dates (petit tableau à droite ou en ligne)
    info_table = Table([
        [Paragraph("<b>N° de facture</b>", style_body),
         Paragraph(invoice_num, style_body)],
        [Paragraph("<b>Date d'émission</b>", style_body),
         Paragraph(issued_str, style_body)],
        [Paragraph("<b>Réservation</b>", style_body),
         Paragraph(ref, style_body)],
    ], colWidths=[3.5*cm, 14*cm])
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, ACW_LIGHT_GREY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 14))

    # 3) Émetteur + Client côte à côte
    issuer_lines = [
        f"<b>{COMPANY_DISPLAY_NAME}</b>",
        f"<font color='#5A6A85'>({COMPANY_LEGAL_NAME})</font>" if COMPANY_LEGAL_NAME and COMPANY_LEGAL_NAME != COMPANY_DISPLAY_NAME else "",
        COMPANY_ADDRESS_LINE1,
        COMPANY_ADDRESS_LINE2,
        f"Tél. {COMPANY_PHONE}",
        f"<a href='mailto:{COMPANY_EMAIL}' color='#03234D'>{COMPANY_EMAIL}</a>",
    ]
    if COMPANY_SIRET:
        issuer_lines.append(f"<font size='8' color='#5A6A85'>SIRET : {_format_siret(COMPANY_SIRET)}</font>")
    if COMPANY_VAT_NUMBER:
        issuer_lines.append(f"<font size='8' color='#5A6A85'>TVA intra. : {COMPANY_VAT_NUMBER}</font>")
    issuer_html = "<br/>".join([l for l in issuer_lines if l])

    client_lines = [f"<b>{client_name}</b>"]
    if client_type == "pro" and client_company:
        client_lines.append(client_company)
    client_lines.append(f"<a href='mailto:{client_email}' color='#03234D'>{client_email}</a>")
    client_html = "<br/>".join(client_lines)

    parties_table = Table([
        [Paragraph("ÉMETTEUR", style_h2), Paragraph("FACTURÉ À", style_h2)],
        [Paragraph(issuer_html, style_body), Paragraph(client_html, style_body)],
    ], colWidths=[8.7*cm, 8.7*cm])
    parties_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
    ]))
    elements.append(parties_table)
    elements.append(Spacer(1, 18))

    # 4) Badge statut "FACTURE ACQUITTÉE" ou "À RÉGLER PAR VIREMENT"
    if payment_method == "stripe":
        status_text = f"✓  FACTURE ACQUITTÉE — Payée le {issued_str} par carte bancaire"
        status_bg = ACW_GREEN
    else:
        status_text = "À RÉGLER PAR VIREMENT BANCAIRE — coordonnées ci-dessous"
        status_bg = ACW_GOLD

    status_table = Table([[Paragraph(status_text, style_status)]], colWidths=[17.4*cm])
    status_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), status_bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(status_table)
    elements.append(Spacer(1, 16))

    # 5) Tableau des prestations
    items_data = [
        ["DESCRIPTION", "QTÉ", "PU HT", "TVA", "MONTANT HT"],
        [description_ligne, "1", _format_money(amount_ht), "20 %", _format_money(amount_ht)],
    ]
    items_table = Table(items_data, colWidths=[8.4*cm, 1.5*cm, 2.3*cm, 1.5*cm, 3.7*cm])
    items_table.setStyle(TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), ACW_NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), rlcolors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        # Body
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), ACW_NAVY),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 1), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 10),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, ACW_LIGHT_GREY),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 10))

    # 6) Bloc des totaux (aligné à droite)
    totals_data = [
        ["Total HT", _format_money(amount_ht)],
        ["TVA 20 %", _format_money(amount_tva)],
        ["TOTAL TTC", _format_money(amount_ttc)],
    ]
    totals_table = Table(totals_data, colWidths=[4.5*cm, 3.7*cm])
    totals_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -2), 10),
        ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("TEXTCOLOR", (0, 0), (-1, -2), ACW_SLATE),
        ("TEXTCOLOR", (0, -1), (-1, -1), ACW_NAVY),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEABOVE", (0, -1), (-1, -1), 1, ACW_NAVY),
        ("TOPPADDING", (0, -1), (-1, -1), 8),
    ]))

    # On positionne le tableau totals à droite via un wrapper
    totals_wrapper = Table([["", totals_table]], colWidths=[9.2*cm, 8.2*cm])
    totals_wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(totals_wrapper)
    elements.append(Spacer(1, 20))

    # 7) Bloc coordonnées bancaires (si virement)
    if payment_method == "virement" and COMPANY_IBAN:
        rib_lines = [f"<b>Coordonnées bancaires pour le virement :</b>"]
        if COMPANY_BANK_NAME:
            rib_lines.append(f"Banque : {COMPANY_BANK_NAME}")
        rib_lines.append(f"IBAN : <font face='Courier'>{COMPANY_IBAN}</font>")
        if COMPANY_BIC:
            rib_lines.append(f"BIC : <font face='Courier'>{COMPANY_BIC}</font>")
        rib_lines.append(f"<font color='#5A6A85'>Merci de rappeler la référence <b>{ref}</b> dans le libellé du virement.</font>")
        rib_block = Paragraph("<br/>".join(rib_lines), style_body)
        rib_wrap = Table([[rib_block]], colWidths=[17.4*cm])
        rib_wrap.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ACW_CREAM),
            ("BOX", (0, 0), (-1, -1), 0.5, ACW_GOLD),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        elements.append(rib_wrap)
        elements.append(Spacer(1, 14))

    # 8) Mentions légales obligatoires
    mentions = [
        "• Paiement comptant à réception de la facture.",
        "• Pas d'escompte accordé pour paiement anticipé.",
        "• En cas de retard de paiement : pénalité égale à 3 fois le taux d'intérêt légal en vigueur.",
        "• Indemnité forfaitaire pour frais de recouvrement : 40 € (art. L441-10 du Code de commerce).",
        "• TVA acquittée sur les encaissements.",
    ]
    mentions_html = "<br/>".join(mentions)
    elements.append(Paragraph("<b>Conditions de paiement & mentions légales</b>", style_h2))
    elements.append(Paragraph(mentions_html, style_legal))
    elements.append(Spacer(1, 20))

    # 9) Footer
    footer_lines = [
        f"{COMPANY_DISPLAY_NAME} — {COMPANY_LEGAL_NAME}" if COMPANY_LEGAL_NAME and COMPANY_LEGAL_NAME != COMPANY_DISPLAY_NAME else COMPANY_DISPLAY_NAME,
        f"{COMPANY_ADDRESS_LINE1} · {COMPANY_ADDRESS_LINE2}",
    ]
    footer_legal = []
    if COMPANY_SIRET:
        footer_legal.append(f"SIRET {_format_siret(COMPANY_SIRET)}")
    if COMPANY_VAT_NUMBER:
        footer_legal.append(f"TVA {COMPANY_VAT_NUMBER}")
    if footer_legal:
        footer_lines.append(" · ".join(footer_legal))
    footer_lines.append(f"<a href='https://{COMPANY_WEBSITE}' color='#C9B584'>{COMPANY_WEBSITE}</a>")

    footer_table = Table(
        [[Paragraph("<br/>".join(footer_lines), style_footer)]],
        colWidths=[17.4*cm],
    )
    footer_table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.5, ACW_LIGHT_GREY),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
    ]))
    elements.append(footer_table)

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


# ============================================================================
# Endpoint FastAPI pour télécharger le PDF de facture
# ============================================================================

@router.get("/api/coworking/invoice/{session_id}.pdf")
async def get_coworking_invoice_pdf(session_id: str):
    """
    Génère et retourne le PDF de facture pour une réservation Stripe.
    URL appelée depuis l'email Resend que reçoit le client.
    Le session_id Stripe sert de token d'accès (unguessable).
    """
    try:
        from pole_sens import supabase  # type: ignore
    except Exception:
        raise HTTPException(status_code=500, detail="Supabase non disponible")

    res = supabase.table("cw_reservations") \
        .select("*") \
        .eq("stripe_session_id", session_id) \
        .limit(1) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Facture introuvable")

    reservation = res.data[0]
    pdf_bytes = generate_coworking_invoice_pdf(reservation, payment_method="stripe")

    ref = reservation.get("reference", "facture")
    invoice_num = ref.replace("RES-", "FAC-") if ref.startswith("RES-") else f"FAC-{ref}"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{invoice_num}.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )


# ============================================================================
# Pour intégrer dans pole_sens.py, ajouter en haut du fichier :
#
#     from webhook_coworking import router as coworking_router
#     app.include_router(coworking_router)
#
# Puis ajouter ces env vars dans Render :
#     STRIPE_WEBHOOK_SECRET_COWORKING=whsec_xxxx
#     STRIPE_SECRET_KEY_COWORKING=sk_test_xxxx (ou sk_live_xxxx)
#     RESEND_API_KEY_COWORKING=re_xxxx
#     IGLOOHOME_DEVICE_ID_COWORKING=xxxx  (optionnel)
#     COWORKING_APP_BASE_URL=https://coworking-sens.com
#     POLE_IAD_SENS_URL=https://pole-iad-sens.fr
#     COMPANY_SIRET=88088657700019
#     COMPANY_VAT_NUMBER=FR85880886577
# ============================================================================
