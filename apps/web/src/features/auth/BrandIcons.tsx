/**
 * Small brand marks for third-party sign-in buttons. lucide-react doesn't
 * ship logos (Google, Feishu, ...), so these are minimal inline SVGs —
 * just enough detail to be recognisable at button-icon size.
 */

export function GoogleIcon({ size = 18 }: { size?: number }) {
  return (
    <svg height={size} viewBox="0 0 48 48" width={size} aria-hidden="true">
      <path
        fill="#4285F4"
        d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11c4.16-3.83 6.56-9.47 6.56-16.17z"
      />
      <path
        fill="#34A853"
        d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 1.32-4.49 2.1-7.45 2.1-5.73 0-10.58-3.87-12.31-9.07H4.34v5.7C7.96 41.07 15.4 46 24 46z"
      />
      <path
        fill="#FBBC05"
        d="M11.69 28.18A13.9 13.9 0 0 1 10.94 24c0-1.45.25-2.86.7-4.18v-5.7H4.34A21.93 21.93 0 0 0 2 24c0 3.54.85 6.89 2.34 9.88l7.35-5.7z"
      />
      <path
        fill="#EA4335"
        d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 24 2 15.4 2 7.96 6.93 4.34 14.12l7.35 5.7c1.73-5.2 6.58-9.07 12.31-9.07z"
      />
    </svg>
  )
}

export function FeishuIcon({ size = 18 }: { size?: number }) {
  // Two overlapping "wing" shapes (a simplified take on Feishu's bird
  // mark) rather than a single letterform — a bare "F" on a blue square
  // reads too close to Facebook's mark at small sizes.
  return (
    <svg height={size} viewBox="0 0 48 48" width={size} aria-hidden="true">
      <path
        fill="#00D6B9"
        d="M4 24.5C4 14 15 6 26 8.5c7 1.6 12 6.9 13.5 13.8.3 1.4-1.1 2.5-2.4 2L23 17.8c-1.7-.6-3.5.6-3.6 2.4l-.5 9.6c-.1 1.9-2.1 3-3.8 2.1L5.4 26.6C4.5 26.1 4 25.3 4 24.5Z"
      />
      <path
        fill="#3370FF"
        d="M44 23.5C44 34 33 42 22 39.5c-7-1.6-12-6.9-13.5-13.8-.3-1.4 1.1-2.5 2.4-2L25 30.2c1.7.6 3.5-.6 3.6-2.4l.5-9.6c.1-1.9 2.1-3 3.8-2.1l9.7 5.3c.9.5 1.4 1.3 1.4 2.1Z"
      />
    </svg>
  )
}
