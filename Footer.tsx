export default function Footer() {
  return (
    <footer className="relative z-10 border-t border-white/10 mt-auto">
      <div className="mx-auto max-w-6xl px-6 py-8 text-center text-xs text-white/55 leading-relaxed">
        <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-1">
          <span>L'Atelier du Coworking</span>
          <span className="text-gold/50">·</span>
          <span>20 rue Pasteur, 89100 Sens</span>
          <span className="text-gold/50">·</span>
          <a
            href="mailto:contact@coworking-sens.com"
            className="text-gold hover:underline"
          >
            contact@coworking-sens.com
          </a>
          <span className="text-gold/50">·</span>
          <a href="tel:+33623880503" className="text-gold hover:underline">
            06.23.88.05.03
          </a>
        </div>
      </div>
    </footer>
  );
}
