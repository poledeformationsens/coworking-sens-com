import type { Config } from "tailwindcss";

export default {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        navy: {
          DEFAULT: "#03234D",
          soft: "#0F3461",
        },
        cream: "#F8F7F4",
        gold: {
          DEFAULT: "#C9B584",
          dark: "#8E764A",
        },
        slate: "#3D4861",
        ink: "#2C2C2A",
      },
      fontFamily: {
        serif: ["'Cormorant Garamond'", "Georgia", "serif"],
        sans: ["'Inter'", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'Geist Mono'", "'SF Mono'", "monospace"],
      },
      letterSpacing: {
        wider2: "0.15em",
        wider3: "0.2em",
      },
    },
  },
  plugins: [],
} satisfies Config;
