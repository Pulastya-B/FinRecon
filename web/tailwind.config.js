/** @type {import('tailwindcss').Config} */

/*
 * The design tokens, in one place.
 *
 * Two things here are load-bearing rather than decorative.
 *
 * THE RAMP. Ten neutrals, not three. Hierarchy in a dense table is made of
 * small distances -- a divider inside a panel must be lighter than the panel's
 * own border, a secondary label must sit between a heading and a disabled
 * control -- and three greys cannot express any of that, so everything landed
 * on the same plane and the screen read flat.
 *
 * THE TYPE SCALE. Size is paired with weight and tracking rather than chosen
 * per element. Negative tracking on large figures is most of the difference
 * between a number that looks designed and one that looks defaulted: 32px at
 * -0.03em reads as a display figure, 32px at 0 reads as body text that grew.
 */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        n: {
          0: '#FFFFFF',    // page
          25: '#FCFCFD',   // panel tint, alternating rows
          50: '#F7F8FA',   // section headers, hover
          100: '#EFF1F5',  // dividers inside panels
          200: '#E3E6EC',  // borders
          300: '#CBD0DA',  // disabled
          500: '#6B7280',  // secondary text
          600: '#4B5563',  // labels
          800: '#1F2430',  // body text
          900: '#0F1420',  // headings, primary figures
        },
        accent: '#0B66EF',
        'accent-bg': '#EEF4FE',
        danger: '#D93025',
        warn: '#B26A00',
      },

      // [size, {lineHeight, letterSpacing, fontWeight}]. The weight travels
      // with the size so a caller cannot pick 32px and forget it is a display
      // figure.
      fontSize: {
        display: ['32px', { lineHeight: '34px', letterSpacing: '-0.03em', fontWeight: '700' }],
        title:   ['20px', { lineHeight: '26px', letterSpacing: '-0.02em', fontWeight: '600' }],
        'body-lg': ['15px', { lineHeight: '20px', letterSpacing: '-0.01em', fontWeight: '500' }],
        body:    ['14px', { lineHeight: '21px', letterSpacing: '0', fontWeight: '400' }],
        'body-sm': ['13px', { lineHeight: '18px', letterSpacing: '0', fontWeight: '400' }],
        label:   ['11px', { lineHeight: '14px', letterSpacing: '0.06em', fontWeight: '600' }],
        mono:    ['13px', { lineHeight: '20px', letterSpacing: '0', fontWeight: '450' }],
      },

      fontFamily: {
        // Inter is self-hosted in src/fonts and declared in index.css, so the
        // stack behind it is a genuine fallback rather than the usual outcome.
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },

      // The 4px grid, named where a number would otherwise be a magic one.
      spacing: {
        gutter: '24px',  // page edge
        panel: '20px',   // inside a panel
        gap: '24px',     // between sections
      },

      height: {
        stat: '72px',    // the stat strip: it holds 32px numerals now
        row: '32px',     // a queue row
      },

      borderRadius: {
        // Nothing above 6px. A dense financial table with soft corners reads as
        // a consumer app pretending to be a tool.
        DEFAULT: '4px',
        md: '4px',
        lg: '6px',
      },
    },
  },
  plugins: [],
}
