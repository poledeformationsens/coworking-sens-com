// Fonction serverless Vercel — Création d'une session Stripe Checkout
// Reçoit les détails de la réservation et renvoie l'URL de paiement Stripe

import Stripe from "stripe";

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY, {
  apiVersion: "2024-12-18.acacia",
});

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Method not allowed" });
  }

  try {
    const { space, slot, date, hourFrom, hourTo, amountTTC, email, clientType, name, company, reference } = req.body || {};

    if (!space || !amountTTC || !email) {
      return res.status(400).json({ error: "Données manquantes (space, amountTTC, email requis)" });
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
    const slotLabel = SLOT_LABELS[slot] || slot;

    const origin = req.headers.origin || "https://coworking-sens.com";

    const session = await stripe.checkout.sessions.create({
      mode: "payment",
      payment_method_types: ["card"],
      line_items: [
        {
          price_data: {
            currency: "eur",
            unit_amount: amountCents,
            product_data: {
              name: `${space} — ${slotLabel}`,
              description: `Réservation L'Atelier du Coworking · ${date || ""}`,
              images: [
                "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo.png",
              ],
            },
          },
          quantity: 1,
        },
      ],
      customer_email: email,
      metadata: {
        space,
        slot,
        date: date || "",
        hourFrom: hourFrom || "",
        hourTo: hourTo || "",
        client_type: clientType || "particulier",
        client_name: name || "",
        company: company || "",
        reference: reference || "",
      },
      success_url: `${origin}/?status=success&session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${origin}/?status=cancelled`,
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
