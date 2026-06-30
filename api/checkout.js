// Fonction serverless Vercel — Création d'une session Stripe Checkout
// Reçoit les détails de la réservation et renvoie l'URL de paiement Stripe

import Stripe from "stripe";

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Method not allowed" });
  }

  try {
    const {
      space, spaceUnit, slot, date, hourFrom, hourTo, amountTTC, email, clientType, name, company, reference, testMode, returnPath,
      // Achat d'un forfait prépayé (pack)
      purchaseType, pricingId, packSpace, packCreditType, packCredits, packLabel,
      // Participation à un événement payant
      eventId, eventTitle, eventDate,
    } = req.body || {};

    const isPack = purchaseType === "pack";
    const isEvent = purchaseType === "event";

    if (!amountTTC || !email || (!isPack && !isEvent && !space)) {
      return res.status(400).json({ error: "Données manquantes (amountTTC, email requis)" });
    }
    if (isPack && (!pricingId || !packSpace || !packCreditType || !packCredits)) {
      return res.status(400).json({ error: "Données forfait manquantes" });
    }
    if (isEvent && !eventId) {
      return res.status(400).json({ error: "Données événement manquantes (eventId requis)" });
    }

    const amountCents = Math.round(amountTTC * 100);
    if (amountCents < 100) {
      return res.status(400).json({ error: "Montant trop bas (minimum 1 €)" });
    }

    // Construction du libellé selon le créneau
    const SLOT_LABELS = {
      morning: "Matinée (8h-12h)",
      afternoon: "Après-midi (14h-18h)",
      day: "Journée (8h-18h)",
      hour: hourFrom && hourTo ? `De ${hourFrom} à ${hourTo}` : "À l'heure",
    };
    // Créneaux modulables : si des horaires précis sont fournis, ils priment sur le libellé fixe
    const SLOT_NAME = { morning: "Demi-journée", afternoon: "Demi-journée", day: "Journée", hour: "À l'heure" };
    const slotLabel = (hourFrom && hourTo)
      ? `${SLOT_NAME[slot] || ""} ${hourFrom}–${hourTo}`.trim()
      : (SLOT_LABELS[slot] || slot);

    const origin = req.headers.origin || "https://coworking-sens.com";

    // Mode test : utilise la clé Stripe TEST si testMode + clé test dispo (sinon LIVE)
    const useTest = testMode && process.env.STRIPE_SECRET_KEY_TEST;
    const stripe = new Stripe(useTest ? process.env.STRIPE_SECRET_KEY_TEST : process.env.STRIPE_SECRET_KEY, {
      apiVersion: "2024-12-18.acacia",
    });

    const productName = isPack
      ? packLabel
      : isEvent
      ? (eventTitle || "Participation événement")
      : `${space} — ${slotLabel}`;
    const productDesc = isPack
      ? `Forfait prépayé · L'Atelier du Coworking`
      : isEvent
      ? `Participation événement · L'Atelier du Coworking${eventDate ? " · " + eventDate : ""}`
      : `Réservation L'Atelier du Coworking · ${date || ""}`;

    const metadata = isPack
      ? {
          purchase_type: "pack",
          pricing_id: pricingId,
          pack_space: packSpace,
          pack_credit_type: packCreditType,
          pack_credits: String(packCredits),
          pack_label: packLabel || "",
          client_type: clientType || "particulier",
          client_name: name || "",
          company: company || "",
          reference: reference || "",
          test_mode: testMode ? "true" : "false",
        }
      : isEvent
      ? {
          purchase_type: "event",
          event_id: String(eventId),
          event_title: eventTitle || "",
          client_name: name || "",
          test_mode: testMode ? "true" : "false",
        }
      : {
          space,
          space_unit: spaceUnit || "",
          slot,
          date: date || "",
          hourFrom: hourFrom || "",
          hourTo: hourTo || "",
          client_type: clientType || "particulier",
          client_name: name || "",
          company: company || "",
          reference: reference || "",
          test_mode: testMode ? "true" : "false",
        };

    const session = await stripe.checkout.sessions.create({
      mode: "payment",
      payment_method_types: ["card"],
      line_items: [
        {
          price_data: {
            currency: "eur",
            unit_amount: amountCents,
            product_data: {
              name: productName,
              description: productDesc,
              images: [
                "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo.png",
              ],
            },
          },
          quantity: 1,
        },
      ],
      customer_email: email,
      metadata,
      success_url: `${origin}${returnPath || "/"}?status=success&session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${origin}${returnPath || "/"}?status=cancelled`,
      locale: "fr",
      billing_address_collection: clientType === "pro" ? "required" : "auto",
      // TVA française incluse — pas de calcul Stripe Tax pour la v1
    });

    return res.status(200).json({ url: session.url, sessionId: session.id });
  } catch (err) {
    console.error("Stripe checkout error:", err);
    return res.status(500).json({
      error: err.message || "Erreur lors de la création de la session de paiement",
    });
  }
}
