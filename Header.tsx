import Link from "next/link";

export default function Header() {
  return (
    <header className="relative z-10 border-b border-white/8">
      <div className="mx-auto max-w-6xl flex items-center justify-between px-6 py-5">
        <Link href="/" className="flex items-center gap-3">
          <img
            src="https://cdn.jsdelivr.net/gh/poledeformationsens/coworking-sens-com@main/acw-logo.png"
            alt="L'Atelier du Coworking"
            className="h-12 w-auto"
          />
          <div className="hidden md:block">
            <div className="font-serif text-lg leading-tight">
              L'Atelier du Coworking
            </div>
            <div className="text-[10px] tracking-[0.3em] text-gold uppercase">
              Sens · Yonne
            </div>
          </div>
        </Link>

        <nav className="hidden md:flex items-center gap-8 text-sm">
          <Link href="/reserver" className="hover:text-gold transition-colors">
            Réserver
          </Link>
          <Link
            href="/devis-privatisation"
            className="hover:text-gold transition-colors"
          >
            Privatisation
          </Link>
          <a
            href="https://coworking-sens.fr"
            className="hover:text-gold transition-colors"
          >
            Découvrir le lieu
          </a>
        </nav>

        <Link
          href="/reserver"
          className="bg-gold text-navy font-serif text-sm tracking-wider2 uppercase px-5 py-2.5 rounded hover:bg-gold-dark hover:text-white transition-colors"
        >
          Réserver
        </Link>
      </div>
    </header>
  );
}
