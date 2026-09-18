// Fonction serverless Vercel — Création d'une session Stripe Checkout
// Reçoit les détails de la réservation et renvoie l'URL de paiement Stripe
//
// Types de paiement gérés (champ purchaseType) :
//   (défaut)       réservation ponctuelle d'un créneau (mode payment, carte)
//   "pack"         achat d'un forfait prépayé — crédits (mode payment, carte)   ← inclut ALC iad 5€/10€
//   "event"        participation à un événement payant (mode payment, carte)
//   "subscription" abonnement Full agent iad 40€/mois (mode subscription, carte + SEPA)
//   "membership"   adhésion Réseau agent iad 120€/an, one-shot annuel (mode payment, carte + SEPA)

import Stripe from "stripe";

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Method not allowed" });
  }

  try {
    const {
      space, spaceUnit, slot, date, hourFrom, hourTo, amountTTC, email, clientType, name, company, reference, testMode, returnPath, comment,
      // Adresse du service comptabilité, quand elle diffère du contact
      billingEmail,
      // Achat d'un forfait prépayé (pack)
      purchaseType, pricingId, packSpace, packCreditType, packCredits, packLabel,
      // Marqueur d'origine (ex. "iad") — propagé jusqu'au forfait pour le cutover 2027
      origin,
      // Abonnement (Full) : intervalle de facturation + crédits mensuels + date d'activation (début de prélèvement)
      billingInterval, monthlyCredits, activationDate,
      // Adhésion (Réseau) : type + durée de validité en mois
      membershipType, membershipMonths,
      // Participation à un événement payant
      eventId, eventTitle, eventDate,
      // Règlement d'un devis accepté en ligne
      devisToken,
    } = req.body || {};

    const isPack = purchaseType === "pack";
    const isEvent = purchaseType === "event";
    const isSubscription = purchaseType === "subscription";
    const isMembership = purchaseType === "membership";
    const isDevis = purchaseType === "devis";

    if (isDevis && !devisToken) {
      return res.status(400).json({ error: "Lien de devis manquant" });
    }
    if (!isDevis && (!amountTTC || !email || (!isPack && !isEvent && !isSubscription && !isMembership && !space))) {
      return res.status(400).json({ error: "Données manquantes (amountTTC, email requis)" });
    }
    if (isPack && (!pricingId || !packSpace || !packCreditType || !packCredits)) {
      return res.status(400).json({ error: "Données forfait manquantes" });
    }
    if (isSubscription && (!packSpace || !packCreditType || !monthlyCredits)) {
      return res.status(400).json({ error: "Données abonnement manquantes" });
    }
    if (isEvent && !eventId) {
      return res.status(400).json({ error: "Données événement manquantes (eventId requis)" });
    }

    // ------------------------------------------------------------------
    // Garde-fou tarifaire : le montant arrive du navigateur et pourrait être
    // modifié dans la console. Pour toute offre au tarif fixe, le serveur
    // impose son propre prix et refuse tout écart.
    // ------------------------------------------------------------------
    const TARIFS_IAD = {
      // abonnement Premium agent iad
      "subscription:full":        40,
      // adhésion Réseau agent iad (annuel one-shot)
      "membership:reseau":        120,
      // formations à la carte, prix UNITAIRE (multiplié par la quantité)
      "pack:iad-alc-half":        5,
      "pack:iad-alc-day":         10,
      "pack:iad-cafe-carnet":     45,
    };

    function tarifAttendu() {
      if (origin !== "iad") return null;
      if (isSubscription && packCreditType === "half-day") {
        return TARIFS_IAD["subscription:full"];
      }
      if (isMembership) {
        const t = TARIFS_IAD["membership:" + (membershipType || "reseau")];
        return t === undefined ? null : t;
      }
      if (isPack) {
        const unitaire = TARIFS_IAD["pack:" + pricingId];
        if (unitaire === undefined) return null;
        // les carnets sont vendus en bloc, les ALC à l'unité
        return pricingId === "iad-cafe-carnet" ? unitaire : unitaire * Number(packCredits || 0);
      }
      return null;
    }

    const attendu = tarifAttendu();
    if (attendu !== null && Math.round(Number(amountTTC) * 100) !== Math.round(attendu * 100)) {
      console.warn(`[CHECKOUT] Tarif refusé — reçu ${amountTTC} €, attendu ${attendu} € (${email})`);
      return res.status(400).json({ error: "Tarif invalide. Rechargez la page et réessayez." });
    }

    // Événement payant : le prix fait foi côté base, jamais côté navigateur.
    let montantFinal = attendu !== null ? attendu : Number(amountTTC);
    if (isEvent) {
      try {
        const r = await fetch(`https://pole-iad-sens.fr/api/coworking/events/${encodeURIComponent(eventId)}/public`);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const ev = await r.json();
        const prix = Number((ev && (ev.event ? ev.event.price_ttc : ev.price_ttc)) || 0);
        if (!(prix > 0)) {
          return res.status(400).json({ error: "Cet événement n'est pas payant." });
        }
        montantFinal = prix;
      } catch (e) {
        console.error("[CHECKOUT] Prix événement illisible :", e);
        return res.status(502).json({ error: "Tarif de l'événement indisponible. Réessayez dans un instant." });
      }
    }

    // Devis : montant et destinataire relus côté serveur depuis le devis lui-même.
    // Le navigateur ne transmet qu'un jeton, jamais un prix.
    let emailDevis = null, refDevis = null;
    if (isDevis) {
      try {
        const r = await fetch(`https://pole-iad-sens.fr/api/coworking/devis/public/${encodeURIComponent(devisToken)}`);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const dv = await r.json();
        const reste = Number(dv.total_ttc || 0) - Number(dv.deja_paye_ttc || 0);
        if (!(reste > 0)) {
          return res.status(400).json({ error: "Ce devis est déjà réglé." });
        }
        montantFinal = reste;
        emailDevis = dv.email || null;
        refDevis = dv.reference || null;
      } catch (e) {
        console.error("[CHECKOUT] Devis illisible :", e);
        return res.status(502).json({ error: "Devis indisponible. Réessayez dans un instant." });
      }
    }

    const amountCents = Math.round(montantFinal * 100);
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

    const reqOrigin = req.headers.origin || "https://coworking-sens.com";

    // Mode test : utilise la clé Stripe TEST si testMode + clé test dispo (sinon LIVE)
    const useTest = testMode && process.env.STRIPE_SECRET_KEY_TEST;
    const stripe = new Stripe(useTest ? process.env.STRIPE_SECRET_KEY_TEST : process.env.STRIPE_SECRET_KEY, {
      apiVersion: "2024-12-18.acacia",
    });

    const originTag = (origin || "").trim().toLowerCase(); // ex. "iad"

    const productName = isPack || isSubscription
      ? packLabel
      : isMembership
      ? (packLabel || "Adhésion Réseau agent iad")
      : isEvent
      ? (eventTitle || "Participation événement")
      : isDevis
      ? `Devis ${refDevis || ""} — L'Atelier du Coworking`.trim()
      : `${space} — ${slotLabel}`;
    const productDesc = isSubscription
      ? `Abonnement mensuel · L'Atelier du Coworking`
      : isMembership
      ? `Adhésion annuelle · L'Atelier du Coworking`
      : isPack
      ? `Forfait prépayé · L'Atelier du Coworking`
      : isEvent
      ? `Participation événement · L'Atelier du Coworking${eventDate ? " · " + eventDate : ""}`
      : isDevis
      ? `Règlement de votre devis · L'Atelier du Coworking`
      : `Réservation L'Atelier du Coworking · ${date || ""}`;

    // ── Métadonnées (portées par la session, et par l'abonnement pour les échéances futures)
    let metadata;
    if (isSubscription) {
      metadata = {
        purchase_type: "subscription",
        origin: originTag || "iad",
        pack_space: packSpace,
        pack_credit_type: packCreditType,
        monthly_credits: String(monthlyCredits),
        pack_label: packLabel || "",
        billing_interval: billingInterval || "month",
        client_name: name || "",
        company: company || "",
        email,
        test_mode: testMode ? "true" : "false",
      };
    } else if (isMembership) {
      metadata = {
        purchase_type: "membership",
        origin: originTag || "iad",
        membership_type: membershipType || "reseau",
        membership_months: String(membershipMonths || 12),
        pack_label: packLabel || "",
        client_name: name || "",
        company: company || "",
        email,
        test_mode: testMode ? "true" : "false",
      };
    } else if (isPack) {
      metadata = {
        purchase_type: "pack",
        origin: originTag,
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
      };
    } else if (isDevis) {
      metadata = {
        purchase_type: "devis",
        devis_token: String(devisToken),
        reference: refDevis || "",
        client_name: name || "",
        test_mode: testMode ? "true" : "false",
      };
    } else if (isEvent) {
      metadata = {
        purchase_type: "event",
        event_id: String(eventId),
        event_title: eventTitle || "",
        client_name: name || "",
        test_mode: testMode ? "true" : "false",
      };
    } else {
      metadata = {
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
        comment: (comment || "").slice(0, 480),
        // Le serveur revalide cette adresse : ici on se contente de la porter.
        billing_email: (billingEmail || "").trim().slice(0, 190),
        test_mode: testMode ? "true" : "false",
      };
    }

    const productData = {
      name: productName,
      description: productDesc,
      images: [
        "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo.png",
      ],
    };

    // ── Session ABONNEMENT (Full 40 €/mois — carte + prélèvement SEPA) ────────
    if (isSubscription) {
      // Date d'activation (optionnelle) : le mandat SEPA est signé maintenant,
      // mais le 1er prélèvement (et donc le crédit des demi-journées) ne se
      // déclenche qu'à cette date, via une "période d'essai" Stripe jusque-là.
      const subscriptionData = { metadata };
      let trialUnix = null;
      if (activationDate) {
        // ~08:00 heure de Paris ce jour-là (≈ 06:00 UTC)
        const t = Math.floor(new Date(`${activationDate}T06:00:00Z`).getTime() / 1000);
        // On n'applique le décalage que si la date est bien dans le futur (> 1 h).
        if (!isNaN(t) && t > Math.floor(Date.now() / 1000) + 3600) {
          trialUnix = t;
          subscriptionData.trial_end = trialUnix;
          metadata.activation_date = activationDate;
        }
      }

      const session = await stripe.checkout.sessions.create({
        mode: "subscription",
        payment_method_types: ["card", "sepa_debit"],
        // Force la collecte du moyen de paiement (mandat SEPA) même avec une
        // période d'essai / activation différée.
        payment_method_collection: "always",
        line_items: [
          {
            price_data: {
              currency: "eur",
              unit_amount: amountCents,
              recurring: { interval: billingInterval || "month" },
              product_data: productData,
            },
            quantity: 1,
          },
        ],
        customer_email: email,
        metadata,
        // Les métadonnées sont recopiées sur l'abonnement : les échéances
        // futures (invoice.paid) n'ont pas accès aux metadata de la session.
        subscription_data: subscriptionData,
        success_url: `${reqOrigin}${returnPath || "/"}${(returnPath || "/").includes("?") ? "&" : "?"}status=success&session_id={CHECKOUT_SESSION_ID}`,
        cancel_url: `${reqOrigin}${returnPath || "/"}${(returnPath || "/").includes("?") ? "&" : "?"}status=cancelled`,
        locale: "fr",
      });
      return res.status(200).json({ url: session.url, sessionId: session.id });
    }

    // ── Session PAIEMENT UNIQUE (réservation / pack / event / adhésion) ───────
    // Adhésion Réseau : carte + SEPA. Le reste : carte uniquement.
    const paymentMethods = isMembership ? ["card", "sepa_debit"] : ["card"];

    const session = await stripe.checkout.sessions.create({
      mode: "payment",
      payment_method_types: paymentMethods,
      line_items: [
        {
          price_data: {
            currency: "eur",
            unit_amount: amountCents,
            product_data: productData,
          },
          quantity: 1,
        },
      ],
      customer_email: emailDevis || email,
      metadata,
      // Si returnPath contient déjà une query (?id=3), on enchaîne avec & au lieu de ?
      success_url: `${reqOrigin}${returnPath || "/"}${(returnPath || "/").includes("?") ? "&" : "?"}status=success&session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${reqOrigin}${returnPath || "/"}${(returnPath || "/").includes("?") ? "&" : "?"}status=cancelled`,
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
