import React from "react";

export interface ButtonProps {
  label: string;
  onClick: () => void;
  variant?: "primary" | "secondary";
  borderRadius?: number;
}

/**
 * Simple button component.
 */
export function Button(props: ButtonProps): React.ReactElement {
  const { label, onClick, variant = "primary", borderRadius = 6 } = props;
  const base: React.CSSProperties = {
    padding: "8px 16px",
    borderRadius,
    border: "none",
    cursor: "pointer",
    background: variant === "primary" ? "#2563eb" : "#e5e7eb",
    color: variant === "primary" ? "#fff" : "#111827",
  };
  return (
    <button style={base} onClick={onClick}>
      {label}
    </button>
  );
}
