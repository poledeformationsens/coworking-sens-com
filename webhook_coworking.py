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
from zoneinfo import ZoneInfo

_PARIS_TZ = ZoneInfo("Europe/Paris")
from fastapi import APIRouter, Request, HTTPException, Response, Header
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
# Secret du webhook Stripe en mode TEST (endpoint test pointant sur la même URL)
STRIPE_WEBHOOK_SECRET_COWORKING_TEST = os.getenv("STRIPE_WEBHOOK_SECRET_COWORKING_TEST", "")

# Clé Stripe API dédiée au coworking (LIVE)
STRIPE_SECRET_KEY_COWORKING = os.getenv("STRIPE_SECRET_KEY_COWORKING", "")
# Clé Stripe API en mode TEST (sk_test_*) — pour facturer les paiements de test
STRIPE_SECRET_KEY_COWORKING_TEST = os.getenv("STRIPE_SECRET_KEY_COWORKING_TEST", "")

# La serrure utilisée pour le coworking (même que pole-iad-sens pour le moment)
IGLOOHOME_DEVICE_ID_COWORKING = os.getenv(
    "IGLOOHOME_DEVICE_ID_COWORKING",
    os.getenv("IGLOOHOME_DEVICE_ID", ""),  # fallback sur celle de pole-iad-sens
)

COWORKING_APP_BASE_URL = os.getenv("COWORKING_APP_BASE_URL", "https://coworking-sens.com")

# URL du backend FastAPI (pour générer le lien vers le PDF facture custom)
POLE_IAD_SENS_URL = os.getenv("POLE_IAD_SENS_URL", "https://pole-iad-sens.fr")

# === Infos entreprise (pour la facture custom) ===
# Coordonnées entreprise — spécifiques à L'Atelier du Coworking Sens
# (Le pôle iad/Viseeon utilisera ses propres variables avec un autre préfixe)
COWORKING_DISPLAY_NAME = os.getenv("COWORKING_DISPLAY_NAME", "L'Atelier du Coworking")
COWORKING_LEGAL_NAME = os.getenv("COWORKING_LEGAL_NAME", "DL CONSULTING")
COWORKING_ADDRESS_LINE1 = os.getenv("COWORKING_ADDRESS_LINE1", "20 rue Pasteur")
COWORKING_ADDRESS_LINE2 = os.getenv("COWORKING_ADDRESS_LINE2", "89100 Sens")
COWORKING_PHONE = os.getenv("COWORKING_PHONE", "+33 6 23 88 05 03")
COWORKING_EMAIL = os.getenv("COWORKING_EMAIL", "contact@coworking-sens.com")
COWORKING_SIRET = os.getenv("COWORKING_SIRET", "88088657700019")  # DL CONSULTING
COWORKING_VAT_NUMBER = os.getenv("COWORKING_VAT_NUMBER", "FR85880886577")
COWORKING_WEBSITE = os.getenv("COWORKING_WEBSITE", "coworking-sens.com")

# RIB coworking — compte bancaire dédié à L'Atelier du Coworking Sens
# (Le pôle iad/Viseeon aura son propre compte plus tard → préfixe COWORKING_ pour éviter toute confusion)
COWORKING_BANK_NAME = os.getenv("COWORKING_BANK_NAME", "")
COWORKING_IBAN = os.getenv("COWORKING_IBAN", "")
COWORKING_BIC = os.getenv("COWORKING_BIC", "")

# === Configuration email — Resend HTTP API (recommandé) ===
# Clé dédiée au domaine coworking-sens.com (séparée de celle utilisée par pole-iad-sens.fr)
RESEND_API_KEY = os.getenv("RESEND_API_KEY_COWORKING", os.getenv("RESEND_API_KEY_CW", ""))
COWORKING_FROM_NAME = os.getenv("COWORKING_FROM_NAME", "L'Atelier du Coworking")
COWORKING_FROM_EMAIL = os.getenv("COWORKING_FROM_EMAIL", "reservation@coworking-sens.com")
COWORKING_REPLY_TO = os.getenv("COWORKING_REPLY_TO", "contact@coworking-sens.com")
# Copie cachée de TOUS les emails envoyés par la plateforme (pour archivage David).
# Vide "" pour désactiver.
COWORKING_BCC = os.getenv("COWORKING_BCC", "david.landry@coworking-sens.com")

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

    # Vérifie la signature contre le secret LIVE puis TEST (le même endpoint reçoit les deux)
    event = None
    last_err = None
    for secret in (STRIPE_WEBHOOK_SECRET_COWORKING, STRIPE_WEBHOOK_SECRET_COWORKING_TEST):
        if not secret:
            continue
        try:
            event = stripe.Webhook.construct_event(payload, sig, secret)
            break
        except stripe.error.SignatureVerificationError as e:
            last_err = e
            continue
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Webhook invalid: {e}")
    if event is None:
        raise HTTPException(status_code=400, detail=f"Webhook invalid: {last_err}")

    etype = event["type"]

    # Paiements ponctuels (réservation, pack, event, adhésion Réseau)
    if etype == "checkout.session.completed":
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

    # Échéances d'abonnement (Full agent iad) : chaque mois payé crédite le forfait
    if etype == "invoice.paid":
        invoice = event["data"]["object"]
        try:
            await _handle_invoice_paid(invoice)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[COWORKING WEBHOOK ERROR invoice.paid] {e}\n{tb}")
        return {"received": True}

    # Échec de prélèvement d'un abonnement : simple notification admin
    if etype == "invoice.payment_failed":
        invoice = event["data"]["object"]
        try:
            _handle_invoice_failed(invoice)
        except Exception as e:
            print(f"[COWORKING WEBHOOK] invoice.payment_failed : {e}")
        return {"received": True}

    # On ignore tous les autres événements
    return {"received": True, "skipped": etype}


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
    space_unit = metadata.get("space_unit", "") or None  # ex: "Bureau 1" / "Bureau 2"
    # Un paiement Stripe en mode TEST (livemode=false) est toujours marqué test_mode en base
    is_stripe_test = not session.get("livemode", True)
    test_mode = (str(metadata.get("test_mode", "")).lower() in ("1", "true", "yes")) or is_stripe_test
    _inv_key = STRIPE_SECRET_KEY_COWORKING_TEST if is_stripe_test else STRIPE_SECRET_KEY_COWORKING

    # ── ABONNEMENT (Premium agent iad) : la session ouvre l'abonnement, mais le
    #    crédit des demi-journées se fait sur l'échéance payée (invoice.paid),
    #    source unique de vérité. On ne crédite donc PAS ici (anti double-crédit).
    #    En revanche on envoie tout de suite un e-mail « abonnement enregistré » :
    #    l'encaissement (et donc le crédit) peut être différé, surtout en SEPA.
    if str(session.get("mode", "")).lower() == "subscription":
        try:
            if (metadata.get("origin", "").lower() == "iad") and customer_email:
                _send_subscription_registered_email(
                    customer_email,
                    metadata.get("client_name") or metadata.get("name") or "",
                    metadata.get("pack_label") or "Forfait Premium agent iad",
                    metadata.get("monthly_credits") or "8",
                )
        except Exception as e:
            print(f"[SUB] Email de souscription non envoyé : {e}")
        return

    # ── Branche FORFAIT (pack prépayé) ───────────────────────────────────────
    # Si l'achat est un forfait, on crédite un portefeuille (cw_packs) et on
    # envoie une facture + un email de confirmation. Pas de réservation ni de PIN.
    if str(metadata.get("purchase_type", "")).lower() == "pack":
        await _handle_pack_purchase(session, metadata, amount_total)
        return

    # ── Branche ADHÉSION RÉSEAU (agent iad, paiement annuel one-shot) ─────────
    if str(metadata.get("purchase_type", "")).lower() == "membership":
        await _handle_membership_purchase(session, metadata, amount_total)
        return

    # ── Branche ÉVÉNEMENT (participation payante) ────────────────────────────
    if str(metadata.get("purchase_type", "")).lower() == "event":
        await _handle_event_registration(session, metadata, amount_total)
        return

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
            pin_start, pin_end = _pin_window(start_dt, end_dt)
            pin_data = igloohome.generate_custom_pin(
                device_id=IGLOOHOME_DEVICE_ID_COWORKING,
                start_date=pin_start,
                end_date=pin_end,
                name=access_name,
            )
            pin_code = pin_data.get("pin_code")
            pin_id = pin_data.get("pin_id")
        except Exception as e:
            print(f"[COWORKING] Erreur génération PIN : {e}")
            # On continue sans PIN, le client recevra son email sans code et appellera le 06.

    # Facturation : émise par NOTRE plateforme (Factur-X), après l'insertion en
    # base pour disposer de l'id de la réservation. Plus aucune facture Stripe.

    # Sauvegarde en Supabase (insertion directe ; dispo re-vérifiée à l'entrée du paiement)
    row = {
        "reference": reference,
        "space": space,
        "space_unit": space_unit,
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
        "pin_id": pin_id,
        "pin_code": pin_code,
        "test_mode": test_mode,
        "status": "confirmed",
        "payment_method": "stripe",
        "comment": (metadata.get("comment") or "").strip() or None,
    }
    resa_id = None
    try:
        from pole_sens import supabase  # type: ignore
        _ins = supabase.table("cw_reservations").insert(row).execute()
        if _ins.data:
            resa_id = _ins.data[0].get("id")
        # Upsert client
        supabase.table("cw_customers").upsert({
            "email": customer_email,
            "name": client_name,
            "company": company or None,
            "last_booking_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="email").execute()
    except Exception as e:
        print(f"[COWORKING] Erreur Supabase : {e}")

    # Émet NOTRE facture (numérotation continue FAC-AAAA-NNNN), sauf Privatisation (devis)
    if space != "Privatisation atelier":
        try:
            from coworking_invoices import issue_invoice
            _slot_lbl = SLOT_LABELS.get(slot, slot)
            if slot == "hour" and hour_from and hour_to:
                _slot_lbl = f"De {hour_from} à {hour_to}"
            _desc = " — ".join([p for p in (space, _slot_lbl) if p])
            issue_invoice(
                source_type="reservation",
                source_id=resa_id,
                stripe_session_id=session.get("id"),
                client_email=customer_email,
                client_name=client_name,
                company=company,
                client_type=client_type,
                description=_desc,
                amount_ttc=amount_total,
                payment_method="stripe",
                test_mode=test_mode,
                space=space,
            )
        except Exception as e:
            print(f"[COWORKING] Erreur émission facture : {e}")

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
    except Exception as e:
        print(f"[COWORKING] Erreur envoi email : {e}")

    # Notification interne au gérant (résumé propre)
    try:
        notif = _build_admin_notif_html(
            "Nouvelle réservation",
            "Une réservation vient d'être réglée en ligne.",
            [
                ("Client", f"{client_name} · {customer_email}"),
                ("Espace", space),
                ("Date", _format_french_date(start_dt)),
                ("Horaire", _format_horaire(slot, hour_from, hour_to)),
                ("Montant", f"{amount_total:.2f}".replace(".", ",") + " € TTC"),
                ("Référence", reference),
            ],
            "Ouvrir le calendrier", f"{COWORKING_APP_BASE_URL}/admin-calendar",
        )
        _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Nouvelle réservation — {space} — {reference}", notif)
    except Exception as e:
        print(f"[COWORKING] Erreur notif admin : {e}")


async def _handle_pack_purchase(session: dict, metadata: dict, amount_total: float):
    """Traite l'achat d'un forfait prépayé : facture Stripe + crédit du
    portefeuille cw_packs + email de confirmation."""
    from coworking_packs import create_pack_from_stripe  # import tardif (évite tout cycle)

    customer_email = session.get("customer_email") or metadata.get("email", "")
    client_name = metadata.get("client_name", "")
    client_type = metadata.get("client_type", "particulier")
    company = metadata.get("company", "")
    pack_label = metadata.get("pack_label", "Forfait")
    reference = metadata.get("reference", _generate_reference())
    _inv_key = STRIPE_SECRET_KEY_COWORKING_TEST if (not session.get("livemode", True)) else STRIPE_SECRET_KEY_COWORKING

    # 1) Crédite le portefeuille
    pack = create_pack_from_stripe(metadata, session, amount_total, None)

    # 2) Émet NOTRE facture (Factur-X, numérotation continue) — plus de facture Stripe
    invoice_pdf_url = None
    try:
        from coworking_invoices import issue_invoice
        pack_id = (pack or {}).get("id")
        issue_invoice(
            source_type="pack",
            source_id=pack_id,
            stripe_session_id=session.get("id"),
            client_email=customer_email,
            client_name=client_name,
            company=company,
            client_type=client_type,
            description=pack_label,
            amount_ttc=amount_total,
            payment_method="stripe",
            test_mode=(not session.get("livemode", True)),
            space=(pack or {}).get("space") or pack_label,
        )
        sid = session.get("id")
        if sid:
            invoice_pdf_url = f"{COWORKING_APP_BASE_URL}/facture-forfait/{sid}.pdf"
    except Exception as e:
        print(f"[PACK] Erreur émission facture : {e}")

    # 3) Email de confirmation au client
    try:
        credits = metadata.get("pack_credits", "")
        credit_word = "journées" if metadata.get("pack_credit_type") == "day" else "demi-journées"
        prenom = _first_name(client_name)
        amount_str = f"{amount_total:.2f}".replace(".", ",") + " € TTC"
        invoice_btn = ""
        if invoice_pdf_url:
            invoice_btn = (
                f'<div style="text-align:center;margin:24px 0;">'
                f'<a href="{invoice_pdf_url}" target="_blank" style="display:inline-block;background:#03234D;'
                f'color:#FFFFFF;text-decoration:none;padding:10px 24px;border-radius:4px;font-family:Arial,sans-serif;'
                f'font-size:12px;letter-spacing:0.1em;text-transform:uppercase;font-weight:600;">Télécharger ma facture</a></div>'
            )
        body = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:28px;text-align:center">
<img src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png" alt="ACW" width="110" height="110" style="display:block;margin:0 auto 10px;border:0;">
<p style="margin:0;color:#C9B584;font-size:12px;letter-spacing:3px;text-transform:uppercase;">Forfait confirmé</p></td></tr>
<tr><td style="padding:30px 32px;">
<p style="font-size:16px;margin:0 0 16px;">Bonjour {prenom},</p>
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">Votre forfait <strong>{pack_label}</strong> est activé. Vous disposez de <strong>{credits} {credit_word}</strong> à utiliser depuis votre espace personnel, sur la page de réservation.</p>
<div style="background:#F8F7F4;border-left:4px solid #C9B584;padding:16px 20px;margin:20px 0;border-radius:4px;">
<p style="margin:0;font-size:14px;line-height:1.6;">Crédits : <strong>{credits} {credit_word}</strong><br>Montant réglé : <strong>{amount_str}</strong><br>Validité : <strong>3 mois</strong> à compter d'aujourd'hui</p></div>
{invoice_btn}
<p style="font-size:14px;line-height:1.6;margin:16px 0 0;">Pour réserver : connectez-vous à votre espace, choisissez votre créneau, puis sélectionnez « Payer avec mon forfait ». Chaque réservation décompte automatiquement 1 crédit.</p>
<div style="text-align:center;margin:24px 0 0;"><a href="{COWORKING_APP_BASE_URL}/mon-espace" style="display:inline-block;background:#C9B584;color:#03234D;text-decoration:none;padding:11px 26px;border-radius:4px;font-size:13px;font-weight:700;">Accéder à mon espace</a></div>
</td></tr>
<tr><td style="background:#F8F7F4;padding:18px 32px;text-align:center;font-size:12px;color:#888;">L'Atelier du Coworking — 20 rue Pasteur, 89100 Sens — 06 23 88 05 03</td></tr>
</table></td></tr></table></body></html>"""
        subject = f"Votre forfait est activé — L'Atelier du Coworking — {pack_label}"
        _send_coworking_email(customer_email, subject, body)
    except Exception as e:
        print(f"[PACK] Erreur email forfait : {e}")

    # Notification interne au gérant
    try:
        credits = metadata.get("pack_credits", "")
        credit_word = "journées" if metadata.get("pack_credit_type") == "day" else "demi-journées"
        notif = _build_admin_notif_html(
            "Nouvel achat de forfait",
            "Un forfait vient d'être acheté en ligne.",
            [
                ("Client", f"{client_name} · {customer_email}"),
                ("Forfait", pack_label),
                ("Crédits", f"{credits} {credit_word}"),
                ("Montant", f"{amount_total:.2f}".replace(".", ",") + " € TTC"),
                ("Référence", reference),
            ],
            "Voir les forfaits", f"{COWORKING_APP_BASE_URL}/admin-forfaits",
        )
        _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Nouvel achat forfait — {pack_label}", notif)
    except Exception as e:
        print(f"[PACK] Erreur notif admin : {e}")


async def _handle_event_registration(session: dict, metadata: dict, amount_total: float):
    """Inscription à un événement réglée via Stripe (participation payante)."""
    from coworking_devis import _supabase, _build_event_confirmation_html, _event_date_fr

    customer_email = (session.get("customer_email") or metadata.get("email", "") or "").strip().lower()
    name = metadata.get("client_name", "") or ""
    try:
        event_id = int(metadata.get("event_id") or 0)
    except (TypeError, ValueError):
        event_id = 0
    if not event_id or not customer_email:
        print(f"[EVENT-PAID] metadata incomplète : event_id={event_id} email={customer_email}")
        return

    sb = _supabase()
    ev = sb.table("cw_events").select("*").eq("id", event_id).limit(1).execute()
    event = ev.data[0] if ev.data else {}
    sid = session.get("id")
    paid_row = {
        "paid": True, "amount_ttc": amount_total, "stripe_session_id": sid,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }

    # Anti-doublon : si déjà inscrit (même email), on marque comme payé, sinon on insère
    existing = sb.table("cw_event_registrations").select("id").eq("event_id", event_id) \
        .ilike("customer_email", customer_email).execute().data or []
    try:
        if existing:
            sb.table("cw_event_registrations").update(paid_row).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("cw_event_registrations").insert(
                {"event_id": event_id, "customer_email": customer_email, "name": name, **paid_row}).execute()
    except Exception as e:
        print(f"[EVENT-PAID] enregistrement : {e}")

    # Email de confirmation au participant
    try:
        html = _build_event_confirmation_html(name, event)
        _send_coworking_email(customer_email, f"Inscription confirmée — {event.get('title')}", html)
    except Exception as e:
        print(f"[EVENT-PAID] email participant : {e}")

    # Notification interne au gérant
    try:
        notif = _build_admin_notif_html(
            "Inscription événement (payante)",
            "Une participation à un événement vient d'être réglée en ligne.",
            [("Participant", f"{name} · {customer_email}"),
             ("Événement", event.get("title") or "—"),
             ("Date", _event_date_fr(event.get("date"), event.get("hour_from"), event.get("hour_to"))),
             ("Montant", f"{amount_total:.2f}".replace(".", ",") + " € TTC")],
            "Voir les inscrits", f"{COWORKING_APP_BASE_URL}/admin-evenements",
        )
        _send_coworking_email(COWORKING_NOTIF_EMAIL, f"[ACW] Inscription payante — {event.get('title')}", notif)
    except Exception as e:
        print(f"[EVENT-PAID] notif admin : {e}")


async def _handle_membership_purchase(session: dict, metadata: dict, amount_total: float):
    """Adhésion Réseau agent iad (paiement annuel one-shot) : active/prolonge
    l'adhésion, auto-inscrit aux mardis Réseau, facture + emails."""
    from coworking_packs import grant_reseau_membership

    customer_email = (session.get("customer_email") or metadata.get("email", "") or "").strip().lower()
    client_name = metadata.get("client_name", "") or ""
    try:
        months = int(metadata.get("membership_months") or 12)
    except (TypeError, ValueError):
        months = 12
    if not customer_email:
        print("[RESEAU] email manquant sur l'adhésion")
        return

    is_stripe_test = not session.get("livemode", True)
    _inv_key = STRIPE_SECRET_KEY_COWORKING_TEST if is_stripe_test else STRIPE_SECRET_KEY_COWORKING

    # Facture Stripe
    invoice_pdf_url = None
    try:
        invoice_data = _create_stripe_invoice(
            customer_email=customer_email, client_name=client_name,
            client_type=metadata.get("client_type", "pro"), company=metadata.get("company", ""),
            space=metadata.get("pack_label", "Adhésion Réseau agent iad"),
            slot="", date_str="", hour_from="", hour_to="",
            amount_ttc=amount_total, reference=_generate_reference(),
            session=session, api_key=_inv_key,
        )
        if invoice_data:
            invoice_pdf_url = invoice_data.get("pdf_url")
    except Exception as e:
        print(f"[RESEAU] Erreur facture Stripe : {e}")

    result = grant_reseau_membership(customer_email, months=months, name=client_name,
                                     session_id=session.get("id"))
    if result.get("duplicate"):
        print(f"[RESEAU] Webhook redélivré ignoré pour {customer_email}")
        return

    # Email au membre
    try:
        prenom = _first_name(client_name)
        until_fr = _format_french_date(datetime.fromisoformat(result["reseau_until"])) if result.get("reseau_until") else ""
        enrolled = result.get("events_enrolled", 0)
        enrolled_line = (f"Vous êtes déjà inscrit(e) à {enrolled} rendez-vous Réseau à venir." if enrolled else
                         "Vous serez inscrit(e) automatiquement à chaque rendez-vous Réseau du mardi.")
        invoice_btn = ""
        if invoice_pdf_url:
            invoice_btn = (f'<div style="text-align:center;margin:24px 0;"><a href="{invoice_pdf_url}" target="_blank" '
                           f'style="display:inline-block;background:#03234D;color:#FFFFFF;text-decoration:none;padding:10px 24px;'
                           f'border-radius:4px;font-family:Arial,sans-serif;font-size:12px;letter-spacing:0.1em;text-transform:uppercase;'
                           f'font-weight:600;">Télécharger ma facture</a></div>')
        body = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:28px;text-align:center">
<p style="margin:0;color:#C9B584;font-size:12px;letter-spacing:3px;text-transform:uppercase;">Adhésion Réseau activée</p></td></tr>
<tr><td style="padding:30px 32px;">
<p style="font-size:16px;margin:0 0 16px;">Bonjour {prenom},</p>
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">Votre adhésion <strong>Réseau agent iad</strong> est active jusqu'au <strong>{until_fr}</strong>. {enrolled_line}</p>
{invoice_btn}
<div style="text-align:center;margin:24px 0 0;"><a href="{COWORKING_APP_BASE_URL}/mon-espace" style="display:inline-block;background:#C9B584;color:#03234D;text-decoration:none;padding:11px 26px;border-radius:4px;font-size:13px;font-weight:700;">Accéder à mon espace</a></div>
</td></tr>
<tr><td style="background:#F8F7F4;padding:18px 32px;text-align:center;font-size:12px;color:#888;">L'Atelier du Coworking — 20 rue Pasteur, 89100 Sens — 06 23 88 05 03</td></tr>
</table></td></tr></table></body></html>"""
        _send_coworking_email(customer_email, "Votre adhésion Réseau est activée — L'Atelier du Coworking", body)
    except Exception as e:
        print(f"[RESEAU] Erreur email membre : {e}")

    # Notif admin
    try:
        notif = _build_admin_notif_html(
            "Nouvelle adhésion Réseau",
            "Une adhésion Réseau agent iad vient d'être réglée.",
            [("Membre", f"{client_name} · {customer_email}"),
             ("Valable jusqu'au", result.get("reseau_until", "—")),
             ("Mardis pré-inscrits", str(result.get("events_enrolled", 0))),
             ("Montant", f"{amount_total:.2f}".replace(".", ",") + " € TTC")],
            "Voir les clients", f"{COWORKING_APP_BASE_URL}/admin-clients")
        _send_coworking_email(COWORKING_NOTIF_EMAIL, "[ACW] Nouvelle adhésion Réseau agent iad", notif)
    except Exception as e:
        print(f"[RESEAU] Erreur notif admin : {e}")


def _send_subscription_registered_email(to_email: str, client_name: str, pack_label: str, credits) -> None:
    """E-mail immédiat de confirmation à la souscription d'un abonnement iad.
    Le crédit des demi-journées interviendra à l'encaissement (immédiat en carte,
    différé de quelques jours en prélèvement SEPA)."""
    prenom = _first_name(client_name) if client_name else ""
    salut = f"Bonjour {prenom}," if prenom else "Bonjour,"
    body = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:28px;text-align:center"><p style="margin:0;color:#C9B584;font-size:12px;letter-spacing:3px;text-transform:uppercase;">Abonnement enregistré</p></td></tr>
<tr><td style="padding:30px 32px;">
<p style="font-size:16px;margin:0 0 16px;">{salut}</p>
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">Votre abonnement <strong>{pack_label}</strong> est bien enregistré. Merci !</p>
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">Vos <strong>{credits} demi-journées</strong> de coworking seront créditées <strong>dès l'encaissement de votre paiement</strong> : immédiat par carte, sous quelques jours en prélèvement SEPA. Vous pourrez alors réserver vos créneaux depuis votre espace.</p>
<div style="text-align:center;margin:24px 0 8px;"><a href="{COWORKING_APP_BASE_URL}/mon-espace" style="display:inline-block;background:#C9B584;color:#03234D;text-decoration:none;padding:11px 26px;border-radius:4px;font-size:13px;font-weight:700;">Accéder à mon espace</a></div>
<p style="font-size:12.5px;color:#5A6A85;line-height:1.6;margin:14px 0 0;">Première connexion ? Créez votre mot de passe avec cette adresse e-mail sur la page « mon espace ».</p>
</td></tr>
<tr><td style="background:#F8F7F4;padding:18px 32px;text-align:center;font-size:12px;color:#888;">L'Atelier du Coworking — 20 rue Pasteur, 89100 Sens — 06 23 88 05 03</td></tr>
</table></td></tr></table></body></html>"""
    _send_coworking_email(to_email, "Votre abonnement est enregistré — L'Atelier du Coworking", body)


async def _handle_invoice_paid(invoice: dict):
    """Échéance d'abonnement payée. Ne traite QUE les factures d'abonnement
    (invoice.subscription renseigné) : les factures que nous générons nous-mêmes
    pour les réservations/forfaits n'ont pas de subscription → ignorées ici."""
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return  # facture manuelle (réservation/pack) : déjà traitée ailleurs

    is_stripe_test = not invoice.get("livemode", True)
    api_key = STRIPE_SECRET_KEY_COWORKING_TEST if is_stripe_test else STRIPE_SECRET_KEY_COWORKING

    # Métadonnées portées par l'abonnement (recopiées via subscription_data.metadata)
    meta = invoice.get("subscription_details", {}).get("metadata") if isinstance(invoice.get("subscription_details"), dict) else None
    if not meta:
        try:
            sub = stripe.Subscription.retrieve(subscription_id, api_key=api_key)
            meta = dict(sub.get("metadata") or {})
        except Exception as e:
            print(f"[FULL] Impossible de lire l'abonnement {subscription_id} : {e}")
            meta = {}

    if (meta.get("origin") or "").lower() != "iad" or (meta.get("purchase_type") or "") != "subscription":
        # Abonnement non géré par ce module
        return

    from coworking_packs import grant_or_refresh_monthly_iad_pack

    email = (invoice.get("customer_email") or meta.get("email", "") or "").strip().lower()
    try:
        credits = int(meta.get("monthly_credits") or 0)
    except (TypeError, ValueError):
        credits = 0
    pack_label = meta.get("pack_label") or "Forfait Full agent iad"
    amount_paid = (invoice.get("amount_paid") or 0) / 100
    # Activation différée : Stripe émet une facture d'essai à 0 € au moment de
    # l'inscription (billing_reason=subscription_create, montant nul) tant que la
    # date d'activation n'est pas atteinte. On ne crédite RIEN sur cette facture :
    # les demi-journées ne sont accordées qu'au 1er prélèvement réel (> 0 €).
    if amount_paid <= 0:
        print(f"[FULL] Facture d'essai/activation différée ({invoice.get('id')}) — 0 €, pas de crédit.")
        return
    test_mode = (str(meta.get("test_mode", "")).lower() in ("1", "true", "yes")) or is_stripe_test
    invoice_id = invoice.get("id")
    invoice_pdf_url = invoice.get("invoice_pdf")

    # Fin de période facturée (expiration du forfait mensuel)
    period_end_iso = None
    try:
        lines = (invoice.get("lines") or {}).get("data") or []
        if lines and lines[0].get("period", {}).get("end"):
            period_end_iso = datetime.fromtimestamp(lines[0]["period"]["end"], tz=timezone.utc).isoformat()
    except Exception:
        period_end_iso = None

    pack = grant_or_refresh_monthly_iad_pack(
        email=email, credits=credits, invoice_id=invoice_id,
        subscription_id=subscription_id, period_end_iso=period_end_iso,
        amount_ttc=amount_paid, test_mode=test_mode, pack_label=pack_label,
        invoice_pdf_url=invoice_pdf_url,
    )
    if pack is None:
        return

    # Email au client (échéance créditée)
    try:
        prenom = _first_name(meta.get("client_name"))
        reason = invoice.get("billing_reason") or ""
        intro = ("Votre abonnement <strong>Full agent iad</strong> est activé."
                 if reason == "subscription_create"
                 else "Votre abonnement <strong>Full agent iad</strong> a été renouvelé pour ce mois.")
        body = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:28px;text-align:center"><p style="margin:0;color:#C9B584;font-size:12px;letter-spacing:3px;text-transform:uppercase;">Forfait mensuel crédité</p></td></tr>
<tr><td style="padding:30px 32px;">
<p style="font-size:16px;margin:0 0 16px;">Bonjour {prenom},</p>
<p style="font-size:15px;line-height:1.6;margin:0 0 16px;">{intro} Vous disposez de <strong>{credits} demi-journées</strong> de coworking ce mois-ci (2 maximum par semaine, non cumulables).</p>
<div style="text-align:center;margin:24px 0 0;"><a href="{COWORKING_APP_BASE_URL}/mon-espace" style="display:inline-block;background:#C9B584;color:#03234D;text-decoration:none;padding:11px 26px;border-radius:4px;font-size:13px;font-weight:700;">Réserver une demi-journée</a></div>
</td></tr>
<tr><td style="background:#F8F7F4;padding:18px 32px;text-align:center;font-size:12px;color:#888;">L'Atelier du Coworking — 20 rue Pasteur, 89100 Sens — 06 23 88 05 03</td></tr>
</table></td></tr></table></body></html>"""
        _send_coworking_email(email, "Votre forfait mensuel est crédité — L'Atelier du Coworking", body)
    except Exception as e:
        print(f"[FULL] Erreur email échéance : {e}")


def _handle_invoice_failed(invoice: dict):
    """Prélèvement d'abonnement échoué : notification admin (pas de crédit)."""
    if not invoice.get("subscription"):
        return
    email = invoice.get("customer_email") or "—"
    amount = (invoice.get("amount_due") or 0) / 100
    try:
        notif = _build_admin_notif_html(
            "Échec de prélèvement (abonnement)",
            "Un prélèvement d'abonnement agent iad a échoué. Le forfait du mois n'a pas été crédité.",
            [("Client", email), ("Montant", f"{amount:.2f}".replace(".", ",") + " € TTC"),
             ("Abonnement", invoice.get("subscription") or "—")],
            "Voir les clients", f"{COWORKING_APP_BASE_URL}/admin-clients")
        _send_coworking_email(COWORKING_NOTIF_EMAIL, "[ACW] Échec prélèvement abonnement agent iad", notif)
    except Exception as e:
        print(f"[FULL] Erreur notif échec : {e}")


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
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """
    Crée une facture Stripe à partir des infos du paiement.
    - Crée/récupère le Customer Stripe
    - Crée un Invoice item + un Invoice (auto-advance)
    - Marque la facture payée (puisque le paiement Stripe Checkout est déjà passé)
    - Stripe envoie le PDF par email au client si "send_invoice"
    Retourne {id, pdf_url, hosted_url} ou None en cas d'erreur.
    """
    # Clé API dédiée (TEST si fournie, sinon LIVE coworking)
    api_key = api_key or STRIPE_SECRET_KEY_COWORKING or None

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


def _pin_window(start_dt, end_dt):
    """Marge d'accès Igloohome : le PIN est actif 15 min AVANT le début et
    30 min APRÈS la fin de la réservation (arrivée en avance / départ tardif)."""
    return start_dt - timedelta(minutes=15), end_dt + timedelta(minutes=30)


def _compute_datetimes(date_str: str, slot: str, hour_from: str, hour_to: str):
    """Retourne (start_dt, end_dt) en UTC, à partir d'horaires LOCAUX Europe/Paris.

    Les horaires affichés au client (8h, 18h…) sont en heure de Paris. On les
    convertit proprement en UTC pour Igloohome (qui réaffiche en heure locale).
    """
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        base = datetime.now() + timedelta(days=1)

    def _parse_hm(s, default_h):
        try:
            parts = str(s).split(":")
            return int(parts[0]), (int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0)
        except Exception:
            return default_h, 0

    # Les horaires explicitement fournis priment toujours (créneaux modulables :
    # journée 8-18/9-19/10-20/11-21, demi-journées décalées, etc.)
    if hour_from and hour_to:
        start_h, start_m = _parse_hm(hour_from, 8)
        end_h, end_m = _parse_hm(hour_to, 18)
    elif slot == "morning":
        (start_h, start_m), (end_h, end_m) = (8, 0), (12, 0)
    elif slot == "afternoon":
        (start_h, start_m), (end_h, end_m) = (14, 0), (18, 0)
    elif slot == "day":
        (start_h, start_m), (end_h, end_m) = (8, 0), (18, 0)
    else:
        (start_h, start_m), (end_h, end_m) = (8, 0), (18, 0)

    # Horaires locaux Europe/Paris → conversion correcte en UTC
    start_local = base.replace(hour=start_h, minute=start_m, second=0, microsecond=0, tzinfo=_PARIS_TZ)
    end_local = base.replace(hour=end_h, minute=end_m, second=0, microsecond=0, tzinfo=_PARIS_TZ)
    start_dt = start_local.astimezone(timezone.utc)
    end_dt = end_local.astimezone(timezone.utc)
    return start_dt, end_dt


def _first_name(full_name: str) -> str:
    """Prénom en sautant une civilité éventuelle (Mme, M., Dr…). Ex: 'Mme Mellie Guerraz' → 'Mellie'."""
    civ = {"m", "mr", "monsieur", "mme", "madame", "mlle", "mademoiselle", "dr", "me", "pr", "maitre", "maître"}
    parts = [p for p in (full_name or "").strip().split() if p]
    while parts and parts[0].lower().strip(".").replace("â", "a") in civ:
        parts.pop(0)
    return parts[0] if parts else "Bonjour"


def _format_french_date(dt: datetime) -> str:
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt.weekday()]} {dt.day} {mois[dt.month - 1]} {dt.year}"


def _format_horaire(slot: str, hour_from: str, hour_to: str) -> str:
    if hour_from and hour_to:
        return f"{hour_from} → {hour_to}"
    if slot == "morning":
        return "8h00 → 12h00"
    if slot == "afternoon":
        return "14h00 → 18h00"
    if slot == "day":
        return "8h00 → 18h00"
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
    offered: bool = False,
) -> str:
    prenom = _first_name(client_name)
    date_long = _format_french_date(start_dt)
    horaire = _format_horaire(slot, hour_from, hour_to)
    amount_str = "Offerte 🎁" if offered else (f"{amount:.2f}".replace(".", ",") + " € TTC")
    gift_block = ""
    if offered:
        gift_block = """
<div style="background:#FDF6E3;border-left:4px solid #C9B584;padding:16px 20px;margin:0 0 8px;border-radius:4px">
  <p style="margin:0;font-size:14px;color:#03234D;line-height:1.6;">🎁 <strong>Cette réservation vous est offerte</strong> par L'Atelier du Coworking. Aucun paiement n'est demandé — profitez bien de votre venue !</p>
</div>
"""

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

    # Règle de calme — uniquement pour l'open space coworking (bureaux privés à préserver)
    quiet_block = ""
    if "coworking" in (space or "").strip().lower():
        quiet_block = """
<div style="background:#FDECEA;border-left:4px solid #EA584A;padding:16px 20px;margin:24px 0;border-radius:4px">
  <p style="margin:0;font-size:14px;color:#03234D;line-height:1.6;">📵 <strong>Merci de ne pas passer d'appel dans l'open space.</strong> Pour préserver le calme des bureaux privés, prenez vos appels dans l'<strong>espace pause portes fermées</strong>, ou dans la <strong>salle de réunion lorsqu'elle est libre</strong>.</p>
</div>
"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F2F2F4;font-family:-apple-system,Arial,sans-serif;color:#03234D;">

<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F2F2F4;padding:30px 12px">
  <tr><td align="center">
    <table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #E5DDCB;">

      <!-- En-tête navy avec logo blanc -->
      <tr><td style="background:#03234D;padding:28px 28px 24px;text-align:center">
        <img src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png" alt="L'Atelier du Coworking" width="120" height="120" style="display:block;margin:0 auto 12px;border:0;outline:0;text-decoration:none;">
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

        {gift_block}

        {pin_block}

        {quiet_block}

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


# Adresse de notification du gérant (toutes les alertes internes)
COWORKING_NOTIF_EMAIL = os.getenv("COWORKING_NOTIF_EMAIL", "david.landry@coworking-sens.com")


def _build_admin_notif_html(title: str, intro: str, rows: list, cta_label: str = None, cta_url: str = None) -> str:
    """Notification interne propre pour le gérant (charte ACW)."""
    trs = ""
    for i, (k, v) in enumerate(rows):
        top = "border-top:1px solid #F0EBE0" if i else ""
        trs += (f'<tr><td style="padding:9px 0;font-size:13.5px;color:#5A6A85;width:150px;{top}">{k}</td>'
                f'<td style="padding:9px 0;font-size:13.5px;color:#03234D;font-weight:600;{top}">{v}</td></tr>')
    cta = ""
    if cta_label and cta_url:
        cta = (f'<table cellpadding="0" cellspacing="0" border="0" style="margin:24px auto 6px"><tr>'
               f'<td align="center" style="border-radius:6px;background:#03234D"><a href="{cta_url}" target="_blank" '
               f'style="display:inline-block;padding:12px 30px;font-family:Arial,sans-serif;font-size:12px;letter-spacing:.12em;'
               f'text-transform:uppercase;font-weight:700;color:#fff;text-decoration:none">{cta_label}</a></td></tr></table>')
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#E9E7E1;font-family:-apple-system,Arial,sans-serif;color:#03234D;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#E9E7E1;padding:30px 12px"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#fff;border:1px solid #E5DDCB;">
<tr><td style="background:#03234D;padding:26px 28px;text-align:center">
<img src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png" alt="ACW" width="80" style="display:block;margin:0 auto 10px;border:0">
<p style="margin:0;font-family:Georgia,serif;font-size:12px;color:#fff;letter-spacing:.22em;text-transform:uppercase">L'Atelier du Coworking</p>
<p style="margin:6px 0 0;font-size:10.5px;color:#C9B584;letter-spacing:.24em;text-transform:uppercase">Notification interne</p></td></tr>
<tr><td style="padding:30px 32px 12px">
<p style="margin:0 0 6px;font-family:Georgia,serif;font-size:19px;color:#03234D">{title}</p>
<p style="margin:0 0 14px;font-size:14px;line-height:1.6;color:#5A6A85">{intro}</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:8px 0 0;border-top:1px solid #E5DDCB;border-bottom:1px solid #E5DDCB">{trs}</table>
{cta}
</td></tr>
<tr><td style="background:#F8F7F4;padding:16px 28px;text-align:center;font-size:11px;color:#8A93A4;border-top:1px solid #E5DDCB">
Notification automatique · L'Atelier du Coworking · 20 rue Pasteur, 89100 Sens</td></tr>
</table></td></tr></table></body></html>"""


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
    # Copie cachée systématique vers la boîte d'archivage (sauf si c'est déjà le destinataire)
    if COWORKING_BCC and COWORKING_BCC.lower() != (to_email or "").lower():
        payload["bcc"] = [COWORKING_BCC]
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
    if COWORKING_BCC and COWORKING_BCC.lower() != (to_email or "").lower():
        msg["Bcc"] = COWORKING_BCC  # send_message enverra aussi au Bcc et retirera l'en-tête
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
# Logo navy (monogramme bleu sur fond crème) — utilisé pour PDF facture (fond clair)
_LOGO_BYTES_CACHE: Optional[bytes] = None
_LOGO_URL = "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-navy.png"
# Logo blanc (monogramme blanc sur fond navy) — utilisé pour bandeau email (fond bleu)
_LOGO_WHITE_URL = "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo-white.png"

# Couleurs ACW
ACW_NAVY = rlcolors.HexColor("#03234D")
ACW_GOLD = rlcolors.HexColor("#C9B584")
ACW_CREAM = rlcolors.HexColor("#F8F7F4")
ACW_SLATE = rlcolors.HexColor("#5A6A85")
ACW_LIGHT_GREY = rlcolors.HexColor("#E5DDCB")
ACW_GREEN = rlcolors.HexColor("#1D9E75")


_LOGO_LOCAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acw-logo-invoice.png")


def _get_logo_bytes() -> Optional[bytes]:
    """Logo ACW pour le PDF facture : fichier local embarqué (bleu transparent,
    sans fond) en priorité, fallback CDN si absent."""
    global _LOGO_BYTES_CACHE
    if _LOGO_BYTES_CACHE is not None:
        return _LOGO_BYTES_CACHE or None
    # 1) Logo embarqué (pas de dépendance réseau, rendu garanti)
    try:
        if os.path.exists(_LOGO_LOCAL_PATH):
            with open(_LOGO_LOCAL_PATH, "rb") as f:
                _LOGO_BYTES_CACHE = f.read()
            return _LOGO_BYTES_CACHE or None
    except Exception as e:
        print(f"[INVOICE PDF] Logo local illisible : {e}")
    # 2) Fallback CDN
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
        author=COWORKING_DISPLAY_NAME,
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
    # Numéro de facture : celui du registre (numérotation continue) s'il existe,
    # sinon dérivé de la référence (compat/anciens documents).
    invoice_num = reservation.get("invoice_number") or (
        ref.replace("RES-", "FAC-") if ref.startswith("RES-") else f"FAC-{ref}"
    )
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

    # Description de la ligne : override possible (ex. forfait) sinon
    # composition espace / créneau / date en ignorant les parties vides.
    item_label = reservation.get("item_label")
    if item_label:
        description_ligne = item_label
    else:
        _parts = [p for p in (space, slot_label, booking_str) if p]
        description_ligne = " — ".join(_parts)

    # === Construction des éléments ===
    elements = []

    # 1) Header : logo à droite + titre à gauche
    logo_bytes = _get_logo_bytes()
    title_block = [
        Paragraph("FACTURE", style_title),
        Paragraph("L'ATELIER DU COWORKING", style_subtitle),
    ]
    if logo_bytes:
        # Logo compact en haut à droite (boîte 4,2 × 3,2 cm, ratio préservé)
        logo_img = Image(io.BytesIO(logo_bytes), width=4.2*cm, height=3.2*cm, kind="proportional")
        logo_img.hAlign = "RIGHT"
        header_table = Table(
            [[title_block, logo_img]],
            colWidths=[11.4*cm, 6*cm]
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        elements.append(header_table)
    else:
        for el in title_block:
            elements.append(el)

    elements.append(Spacer(1, 4))

    # 2) Numéro de facture + dates (petit tableau à droite ou en ligne)
    info_rows = [
        [Paragraph("<b>N° de facture</b>", style_body),
         Paragraph(invoice_num, style_body)],
        [Paragraph("<b>Date d'émission</b>", style_body),
         Paragraph(issued_str, style_body)],
    ]
    # Ligne de référence : "Réservation" pour une résa, "Forfait" pour un forfait
    _ref_label = None
    if ref and str(ref).startswith("RES-"):
        _ref_label = "Réservation"
    elif ref and str(ref).startswith("FORF-"):
        _ref_label = "Forfait"
    if _ref_label:
        info_rows.append([Paragraph(f"<b>{_ref_label}</b>", style_body),
                          Paragraph(ref, style_body)])
    info_table = Table(info_rows, colWidths=[3.5*cm, 14*cm])
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, ACW_LIGHT_GREY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 10))

    # 3) Émetteur + Client côte à côte
    issuer_lines = [
        f"<b>{COWORKING_DISPLAY_NAME}</b>",
        f"<font color='#5A6A85'>({COWORKING_LEGAL_NAME})</font>" if COWORKING_LEGAL_NAME and COWORKING_LEGAL_NAME != COWORKING_DISPLAY_NAME else "",
        COWORKING_ADDRESS_LINE1,
        COWORKING_ADDRESS_LINE2,
        f"Tél. {COWORKING_PHONE}",
        f"<a href='mailto:{COWORKING_EMAIL}' color='#03234D'>{COWORKING_EMAIL}</a>",
    ]
    if COWORKING_SIRET:
        issuer_lines.append(f"<font size='8' color='#5A6A85'>SIRET : {_format_siret(COWORKING_SIRET)}</font>")
    if COWORKING_VAT_NUMBER:
        issuer_lines.append(f"<font size='8' color='#5A6A85'>TVA intra. : {COWORKING_VAT_NUMBER}</font>")
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
    elements.append(Spacer(1, 12))

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
    elements.append(Spacer(1, 10))

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
    elements.append(Spacer(1, 12))

    # 7) Bloc coordonnées bancaires (si virement) — compte coworking dédié
    if payment_method == "virement" and COWORKING_IBAN:
        rib_lines = [f"<b>Coordonnées bancaires pour le virement :</b>"]
        if COWORKING_BANK_NAME:
            rib_lines.append(f"Banque : {COWORKING_BANK_NAME}")
        rib_lines.append(f"IBAN : <font face='Courier'>{COWORKING_IBAN}</font>")
        if COWORKING_BIC:
            rib_lines.append(f"BIC : <font face='Courier'>{COWORKING_BIC}</font>")
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
    elements.append(Spacer(1, 12))

    # 9) Footer
    footer_lines = [
        f"{COWORKING_DISPLAY_NAME} — {COWORKING_LEGAL_NAME}" if COWORKING_LEGAL_NAME and COWORKING_LEGAL_NAME != COWORKING_DISPLAY_NAME else COWORKING_DISPLAY_NAME,
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
        ("TOPPADDING", (0, 0), (-1, -1), 10),
    ]))
    elements.append(footer_table)

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()

    # === Factur-X : embarque le XML CII EN 16931 → PDF/A-3 hybride ===
    # (réforme facturation électronique). En cas d'échec, PDF simple renvoyé.
    try:
        from coworking_facturx import to_facturx
        pdf_bytes = to_facturx(pdf_bytes, reservation, payment_method)
    except Exception as _fx_err:
        print(f"[FACTURX] wrapper non appliqué : {_fx_err}")

    return pdf_bytes


def generate_coworking_avoir_pdf(reservation: dict, avoir_ref: str, amount_ttc: float,
                                 reason: str = "", refund_method: str = "stripe") -> bytes:
    """Génère le PDF d'AVOIR (note de crédit) L'Atelier du Coworking.
    refund_method : 'stripe' (remboursé carte) | 'virement' (à rembourser par virement)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=1.6*cm, bottomMargin=1.6*cm,
                            title=f"Avoir {avoir_ref}", author=COWORKING_DISPLAY_NAME)

    st_title = ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=28, textColor=ACW_NAVY, leading=32)
    st_sub = ParagraphStyle("S", fontName="Helvetica", fontSize=9, textColor=ACW_GOLD, leading=12, spaceAfter=14)
    st_body = ParagraphStyle("B", fontName="Helvetica", fontSize=9.5, textColor=ACW_NAVY, leading=14)
    st_small = ParagraphStyle("Sm", fontName="Helvetica", fontSize=8, textColor=ACW_SLATE, leading=11)
    st_legal = ParagraphStyle("L", fontName="Helvetica", fontSize=7.5, textColor=ACW_SLATE, leading=10, alignment=TA_LEFT)
    st_footer = ParagraphStyle("F", fontName="Helvetica", fontSize=8, textColor=ACW_SLATE, leading=11, alignment=TA_CENTER)

    ref = reservation.get("reference", "")
    invoice_num = ref.replace("RES-", "FAC-") if ref.startswith("RES-") else (f"FAC-{ref}" if ref else "—")
    amount_ttc = round(float(amount_ttc or 0), 2)
    amount_ht = round(amount_ttc / 1.20, 2)
    amount_tva = round(amount_ttc - amount_ht, 2)
    space = reservation.get("space", "")
    slot = reservation.get("slot", "")
    date_str = reservation.get("date", "")
    try:
        booking_str = _format_french_date(datetime.strptime(date_str, "%Y-%m-%d"))
    except Exception:
        booking_str = date_str
    slot_label = SLOT_LABELS.get(slot, slot)
    issued_str = _format_french_date(datetime.now(timezone.utc))

    elements = []
    logo_bytes = _get_logo_bytes()
    title_block = [Paragraph("AVOIR", st_title), Paragraph("L'ATELIER DU COWORKING", st_sub)]
    if logo_bytes:
        logo_img = Image(io.BytesIO(logo_bytes), width=6*cm, height=6*cm, kind="proportional")
        ht = Table([[title_block, logo_img]], colWidths=[10*cm, 7.4*cm])
        ht.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
        elements.append(ht)
    else:
        elements.extend(title_block)
    elements.append(Spacer(1, 6))

    info = Table([
        [Paragraph("<b>N° d'avoir</b>", st_body), Paragraph(avoir_ref, st_body)],
        [Paragraph("<b>Date d'émission</b>", st_body), Paragraph(issued_str, st_body)],
        [Paragraph("<b>Facture annulée</b>", st_body), Paragraph(invoice_num, st_body)],
        [Paragraph("<b>Réservation</b>", st_body), Paragraph(ref or "—", st_body)],
    ], colWidths=[3.5*cm, 14*cm])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LINEBELOW", (0, -1), (-1, -1), 0.5, ACW_LIGHT_GREY),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    elements.append(info)
    elements.append(Spacer(1, 14))

    issuer = "<br/>".join([x for x in [
        f"<b>{COWORKING_DISPLAY_NAME}</b>",
        f"({COWORKING_LEGAL_NAME})" if COWORKING_LEGAL_NAME and COWORKING_LEGAL_NAME != COWORKING_DISPLAY_NAME else "",
        COWORKING_ADDRESS_LINE1, COWORKING_ADDRESS_LINE2,
        f"SIRET {COWORKING_SIRET}" if COWORKING_SIRET else "",
    ] if x])
    client = "<br/>".join([x for x in [
        f"<b>{reservation.get('name', '')}</b>",
        reservation.get("company") or "",
        reservation.get("email", ""),
    ] if x])
    party = Table([[Paragraph("ÉMETTEUR", st_small), Paragraph("CLIENT", st_small)],
                  [Paragraph(issuer, st_body), Paragraph(client, st_body)]], colWidths=[8.7*cm, 8.7*cm])
    party.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 1), (-1, 1), 2)]))
    elements.append(party)
    elements.append(Spacer(1, 16))

    rows = [
        [Paragraph("<b>Description</b>", st_body), Paragraph("<b>Montant TTC</b>", st_body)],
        [Paragraph(f"Annulation — {space} — {slot_label} — {booking_str}", st_body),
         Paragraph(f"- {_format_money(amount_ttc)}", st_body)],
        [Paragraph("Total HT", st_small), Paragraph(f"- {_format_money(amount_ht)}", st_small)],
        [Paragraph("TVA 20 %", st_small), Paragraph(f"- {_format_money(amount_tva)}", st_small)],
        [Paragraph("<b>TOTAL AVOIR TTC</b>", st_body), Paragraph(f"<b>- {_format_money(amount_ttc)}</b>", st_body)],
    ]
    t = Table(rows, colWidths=[13.4*cm, 4*cm])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, ACW_NAVY),
        ("LINEBELOW", (0, 1), (-1, 1), 0.5, ACW_LIGHT_GREY),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, ACW_NAVY),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 16))

    if refund_method == "stripe":
        mention = "Remboursement effectué sur la carte bancaire via Stripe."
    elif refund_method == "virement":
        mention = "Remboursement par virement bancaire. Merci de nous communiquer votre IBAN si ce n'est pas déjà fait."
    else:
        mention = "Avoir émis suite à l'annulation de la réservation."
    elements.append(Paragraph(mention, st_body))
    if reason:
        elements.append(Spacer(1, 6))
        elements.append(Paragraph(f"<b>Motif :</b> {reason}", st_small))
    elements.append(Spacer(1, 20))

    legal = (f"{COWORKING_DISPLAY_NAME}"
             + (f" — {COWORKING_LEGAL_NAME}" if COWORKING_LEGAL_NAME else "")
             + f" — {COWORKING_ADDRESS_LINE1}, {COWORKING_ADDRESS_LINE2}"
             + (f" — SIRET {COWORKING_SIRET}" if COWORKING_SIRET else "")
             + ". TVA non applicable, art. 293 B du CGI (si franchise).")
    elements.append(Paragraph(legal, st_legal))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"{COWORKING_DISPLAY_NAME} · {COWORKING_PHONE} · {COWORKING_EMAIL} · coworking-sens.com", st_footer))

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
    invoice_num = reservation.get("invoice_number") or (
        ref.replace("RES-", "FAC-") if ref.startswith("RES-") else f"FAC-{ref}"
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{invoice_num}.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/api/coworking/invoice-forfait/{session_id}.pdf")
async def get_coworking_pack_invoice_pdf(session_id: str):
    """Facture Factur-X d'un achat de FORFAIT — générée par notre plateforme.
    Le stripe_session_id sert de jeton d'accès (non devinable)."""
    try:
        from pole_sens import supabase  # type: ignore
    except Exception:
        raise HTTPException(status_code=500, detail="Supabase non disponible")

    res = supabase.table("cw_packs").select("*") \
        .eq("stripe_session_id", session_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Facture introuvable")
    pack = res.data[0]

    # Nom / société depuis la fiche client
    cust_name, cust_company = pack.get("customer_email", ""), None
    try:
        c = supabase.table("cw_customers").select("name,company") \
            .ilike("email", pack.get("customer_email", "")).limit(1).execute()
        if c.data:
            cust_name = c.data[0].get("name") or cust_name
            cust_company = c.data[0].get("company")
    except Exception:
        pass

    label = pack.get("label") or "Forfait"
    invoice_number = pack.get("invoice_number")
    inv = {
        "reference": pack.get("reference") or "",
        "invoice_number": invoice_number,
        "item_label": label,
        "space": label,
        "slot": "",
        "date": "",
        "amount_ttc": pack.get("amount_ttc", 0),
        "created_at": pack.get("purchased_at"),
        "name": cust_name,
        "email": pack.get("customer_email", ""),
        "company": cust_company,
        "client_type": "pro" if cust_company else "particulier",
    }
    pdf_bytes = generate_coworking_invoice_pdf(inv, payment_method="stripe")
    fname = (invoice_number or "facture-forfait")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{fname}.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )


# ============================================================================
# Admin : codes d'accès d'une réservation (afficher / renvoyer)
# ============================================================================
def _reservation_access_whatsapp_message(resa: dict) -> str:
    """Message WhatsApp prêt à envoyer avec le code d'accès."""
    prenom = _first_name(resa.get("name"))
    space = resa.get("space") or "votre espace"
    date_str = resa.get("date") or ""
    hf, ht = resa.get("hour_from"), resa.get("hour_to")
    pin = resa.get("pin_code")
    horaire = f" · {hf}–{ht}" if hf and ht else ""
    lines = [
        f"Bonjour {prenom},", "",
        "Voici vos accès pour votre réservation à L'Atelier du Coworking :",
        f"- Espace : {space}",
        f"- Date : {date_str}{horaire}",
        (f"- Code d'accès : {pin}" if pin else "- Code d'accès : nous vous le communiquons au plus vite"),
        "- Wifi : Coworkingsens / Cowork2023@@",
        "",
        "Accès : au 20 rue Pasteur (89100 Sens), tapez le code sur le clavier de la porte puis appuyez sur la touche cadenas. Patientez 2 secondes, la porte s'ouvre.",
        "À très vite !",
        "L'Atelier du Coworking",
    ]
    return "\n".join(lines)


def _load_reservation_or_404(resa_id: str) -> dict:
    from pole_sens import supabase  # type: ignore
    res = supabase.table("cw_reservations").select("*").eq("id", resa_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Réservation introuvable")
    return res.data[0]


@router.get("/api/coworking/admin/reservations/{resa_id}/access")
async def admin_reservation_access(resa_id: str, authorization: Optional[str] = Header(None)):
    """Code Igloohome + coordonnées + message WhatsApp prêt à envoyer (admin)."""
    from coworking_devis import _check_admin  # type: ignore
    _check_admin(authorization)
    resa = _load_reservation_or_404(resa_id)
    email = (resa.get("email") or "").strip()
    phone = None
    try:
        from pole_sens import supabase  # type: ignore
        if email:
            c = supabase.table("cw_customers").select("phone").ilike("email", email).limit(1).execute()
            if c.data:
                phone = c.data[0].get("phone")
    except Exception:
        phone = None
    return {
        "reference": resa.get("reference"),
        "name": resa.get("name"),
        "email": email,
        "phone": phone,
        "space": resa.get("space"),
        "date": resa.get("date"),
        "hour_from": resa.get("hour_from"),
        "hour_to": resa.get("hour_to"),
        "pin_code": resa.get("pin_code"),
        "comment": resa.get("comment"),
        "whatsapp_message": _reservation_access_whatsapp_message(resa),
    }


@router.post("/api/coworking/admin/reservations/{resa_id}/resend-email")
async def admin_reservation_resend_email(resa_id: str, authorization: Optional[str] = Header(None)):
    """Renvoie l'email de confirmation (avec le code d'accès) au client."""
    from coworking_devis import _check_admin  # type: ignore
    _check_admin(authorization)
    resa = _load_reservation_or_404(resa_id)
    email = (resa.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Aucun email client sur la réservation")
    start_dt, end_dt = _compute_datetimes(
        resa.get("date") or "", resa.get("slot") or "",
        resa.get("hour_from") or "", resa.get("hour_to") or "",
    )
    sid = resa.get("stripe_session_id")
    invoice_url = f"{COWORKING_APP_BASE_URL}/facture/{sid}.pdf" if sid else None
    html = _build_confirmation_email_html(
        client_name=resa.get("name") or "",
        reference=resa.get("reference") or "",
        space=resa.get("space") or "",
        slot=resa.get("slot") or "",
        date_str=resa.get("date") or "",
        hour_from=resa.get("hour_from") or "",
        hour_to=resa.get("hour_to") or "",
        amount=float(resa.get("amount_ttc") or 0),
        pin_code=resa.get("pin_code"),
        start_dt=start_dt,
        end_dt=end_dt,
        invoice_pdf_url=invoice_url,
    )
    subject = f"Vos codes d'accès — L'Atelier du Coworking — {resa.get('reference')}"
    _send_coworking_email(email, subject, html)
    return {"ok": True, "email": email}


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
#     COWORKING_DISPLAY_NAME=L'Atelier du Coworking
#     COWORKING_LEGAL_NAME=DL CONSULTING
#     COWORKING_ADDRESS_LINE1=20 rue Pasteur
#     COWORKING_ADDRESS_LINE2=89100 Sens
#     COWORKING_PHONE=+33 6 23 88 05 03
#     COWORKING_EMAIL=contact@coworking-sens.com
#     COWORKING_SIRET=88088657700019
#     COWORKING_VAT_NUMBER=FR85880886577
#     COWORKING_WEBSITE=coworking-sens.com
#     COWORKING_BANK_NAME=CIC SENS         (pour virements/devis)
#     COWORKING_IBAN=FR76 xxxx xxxx ...    (pour virements/devis)
#     COWORKING_BIC=CMCIFRPP                (pour virements/devis)
# ============================================================================
