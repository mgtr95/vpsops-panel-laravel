export default function Logo({ size = 28, showText = true, className = "" }) {
  return (
    <span className={`logo${className ? ` ${className}` : ""}`}>
      <svg
        className="logo-mark"
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
      >
        <rect x="4" y="5" width="24" height="10" rx="2.5" stroke="currentColor" strokeWidth="1.75" />
        <rect x="4" y="17" width="24" height="10" rx="2.5" stroke="currentColor" strokeWidth="1.75" />
        <circle cx="9" cy="10" r="1.75" fill="#3d9a6a" />
        <circle cx="9" cy="22" r="1.75" fill="#3d9a6a" />
        <line x1="13" y1="10" x2="24" y2="10" stroke="#4db8c4" strokeWidth="1.75" strokeLinecap="round" />
        <line x1="13" y1="22" x2="19" y2="22" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" opacity="0.35" />
      </svg>
      {showText && (
        <span className="logo-text">
          VPS<span className="logo-accent">ops</span>
        </span>
      )}
    </span>
  );
}
