import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "L'Atelier du Coworking — Réservation à Sens",
  description:
    "Réservez un bureau privé, une salle de réunion ou un espace coworking à L'Atelier du Coworking, 20 rue Pasteur, Sens (89).",
  openGraph: {
    title: "L'Atelier du Coworking — Sens",
    description:
      "Bureaux privés, salle de réunion, coworking et privatisations. 20 rue Pasteur, 89100 Sens.",
    url: "https://coworking-sens.com",
    type: "website",
    images: [
      "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo.png",
    ],
  },
  icons: {
    icon: "/favicon.png",
    apple: "/apple-touch-icon.png",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="fr">
      <body>
        <div className="acw-bg" aria-hidden="true" />
        <div className="acw-overlay" aria-hidden="true" />
        {children}
      </body>
    </html>
  );
}
