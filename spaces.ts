export type Slot = "morning" | "afternoon" | "day" | "hour";

export type Space = {
  slug: string;
  name: string;
  description: string;
  photo: string;
  capacity: string;
  prices: { hour: number; morning: number; afternoon: number; day: number };
  onRequest?: boolean;
};

const LOGO_BASE =
  "https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main";

export const SPACES: Space[] = [
  {
    slug: "bureau-1",
    name: "Bureau 1",
    description: "Bureau privé · 1 à 2 personnes",
    photo: `${LOGO_BASE}/Bureau%201.jpg`,
    capacity: "2 personnes max",
    prices: { hour: 8, morning: 16, afternoon: 16, day: 29 },
  },
  {
    slug: "bureau-2",
    name: "Bureau 2",
    description: "Bureau privé · 1 à 2 personnes",
    photo: `${LOGO_BASE}/Bureau%201.jpg`,
    capacity: "2 personnes max",
    prices: { hour: 8, morning: 16, afternoon: 16, day: 29 },
  },
  {
    slug: "salle-reunion",
    name: "Salle de réunion",
    description: "Jusqu'à 14 en réunion, 20 en théâtre",
    photo: `${LOGO_BASE}/salle%20de%20r%C3%A9union.jpg`,
    capacity: "14 à 20 personnes",
    prices: { hour: 29, morning: 70, afternoon: 70, day: 130 },
  },
  {
    slug: "coworking",
    name: "Coworking",
    description: "Poste en open space",
    photo: `${LOGO_BASE}/Photo%20principale.jpg`,
    capacity: "Place ouverte",
    prices: { hour: 4, morning: 8, afternoon: 8, day: 15 },
  },
  {
    slug: "privatisation",
    name: "Privatisation",
    description: "Atelier complet · événements & formations",
    photo: `${LOGO_BASE}/Photo%20principale.jpg`,
    capacity: "Jusqu'à 30 personnes",
    prices: { hour: 45, morning: 0, afternoon: 0, day: 0 },
    onRequest: true,
  },
];

export const SLOT_LABELS: Record<Slot, string> = {
  morning: "Matinée (8h-12h)",
  afternoon: "Après-midi (14h-18h)",
  day: "Journée (8h-18h)",
  hour: "À l'heure",
};

export const TVA = 0.2;

export function ttc(ht: number) {
  return Math.round(ht * (1 + TVA) * 100) / 100;
}
