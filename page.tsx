import Header from "@/components/Header";
import Footer from "@/components/Footer";
import Link from "next/link";

export default function ConfirmationPage() {
  const ref = "RES-2026-0042";
  const email = "sophie@exemple.fr";
  const space = "Bureau 1";
  const date = "mardi 26 mai 2026";
  const horaire = "8h00 → 18h00";
  const montant = "34,80 €";

  const startISO = "20260526";
  const gcalUrl = `https://calendar.google.com/calendar/render?action=TEMPLATE&text=${encodeURIComponent(
    `Réservation ${space} — L'Atelier du Coworking`
  )}&dates=${startISO}T080000/${startISO}T180000&details=${encodeURIComponent(
    `Référence ${ref}. Code d'accès envoyé par e-mail. Wifi : Coworkingsens / Cowork2023@@`
  )}&location=${encodeURIComponent("20 rue Pasteur, 89100 Sens")}`;

  const mapsUrl =
    "https://www.google.com/maps/dir/?api=1&destination=20+rue+Pasteur+89100+Sens";

  return (
    <div className="min-h-screen flex flex-col">
      <Header />
      <main className="relative z-10 flex-1">
        <div className="mx-auto max-w-2xl px-6 py-12">
          <div className="text-center mb-6">
            <div className="inline-flex items-center justify-center w-14 h-14 rounded-full border-2 border-gold mb-5">
              <svg viewBox="0 0 24 24" fill="none" className="w-7 h-7 text-gold" stroke="currentColor" strokeWidth="2.5">
                <path d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <h1 className="font-serif text-3xl mb-2">Réservation confirmée</h1>
            <p className="text-[10px] tracking-[0.3em] text-gold uppercase">
              Référence {ref}
            </p>
            <div className="mx-auto w-12 h-px bg-gold/40 mt-5" aria-hidden="true" />
          </div>

          <Section title="Votre réservation" icon="calendar">
            <Row label="Espace" value={space} />
            <Row label="Date" value={date} />
            <Row label="Horaire" value={horaire} />
            <Row label="Montant payé" value={`${montant} TTC`} />
          </Section>

          <Section title="Votre code d'accès" icon="mail">
            <p className="text-sm leading-relaxed text-white/85">
              Le code d'accès et les instructions vous ont été envoyés par e-mail à{" "}
              <span className="font-mono text-gold text-xs">{email}</span>.
            </p>
            <p className="text-xs text-white/55 italic mt-2">
              Vérifiez aussi vos courriers indésirables. En cas de problème,
              appelez-nous au 06.23.88.05.03.
            </p>
          </Section>

          <Section title="Informations pratiques" icon="pin">
            <Row label="Adresse" value="20 rue Pasteur · 89100 Sens" />
            <Row label="Wifi" value="Coworkingsens · Cowork2023@@" />
            <Row label="Contact" value="06.23.88.05.03" />
            <div className="grid sm:grid-cols-2 gap-2 mt-4">
              <a
                href={gcalUrl}
                target="_blank"
                rel="noopener"
                className="font-serif text-xs tracking-wider2 uppercase text-center border border-gold/50 text-white px-4 py-3 rounded hover:bg-gold/10 transition-colors"
              >
                Mon agenda
              </a>
              <a
                href={mapsUrl}
                target="_blank"
                rel="noopener"
                className="font-serif text-xs tracking-wider2 uppercase text-center border border-gold/50 text-white px-4 py-3 rounded hover:bg-gold/10 transition-colors"
              >
                Itinéraire
              </a>
            </div>
          </Section>

          <div className="text-center mt-6">
            <Link
              href="/reserver"
              className="inline-block font-serif text-xs tracking-wider2 uppercase bg-gold text-navy px-6 py-3 rounded hover:bg-gold-dark hover:text-white transition-colors"
            >
              Faire une autre réservation
            </Link>
          </div>

          <p className="text-center text-xs text-white/55 mt-6">
            Une facture <span className="font-mono text-gold">FAC-2026-0042</span>{" "}
            a été envoyée à <span className="font-mono text-gold">{email}</span>.
          </p>
        </div>
      </main>
      <Footer />
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  icon: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white/5 border border-gold/20 rounded-lg p-5 mb-3">
      <p className="text-[10px] tracking-[0.22em] uppercase font-serif text-gold mb-3">
        {title}
      </p>
      {children}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-sm py-1">
      <span className="text-white/62">{label}</span>
      <span>{value}</span>
    </div>
  );
}
