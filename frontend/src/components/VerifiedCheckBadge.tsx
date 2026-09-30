// ---------------------------------------------------------------------------
// Transfera v2 — Verified Check Badge (GitHub CI-style)
// Displays a crisp green tick badge indicating BLAKE3 hash verification.
// ---------------------------------------------------------------------------

import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface VerifiedCheckBadgeProps {
  size?: "sm" | "md" | "lg";
  showLabel?: boolean;
  labelText?: string;
  className?: string;
  title?: string;
}

export function VerifiedCheckBadge({
  size = "sm",
  showLabel = false,
  labelText = "Verified",
  className,
  title = "All hashes verified (BLAKE3 cryptographic integrity confirmed)",
}: VerifiedCheckBadgeProps) {
  const circleSizes = {
    sm: "w-3.5 h-3.5",
    md: "w-4 h-4",
    lg: "w-5 h-5",
  };

  const iconSizes = {
    sm: "w-2.5 h-2.5",
    md: "w-3 h-3",
    lg: "w-3.5 h-3.5",
  };

  const iconOnly = (
    <span
      className={cn(
        "rounded-full bg-emerald-500 text-white flex items-center justify-center shrink-0 shadow-xs",
        circleSizes[size],
      )}
      title={title}
      aria-label={title}
    >
      <Check className={cn("stroke-[3]", iconSizes[size])} />
    </span>
  );

  if (!showLabel) {
    return (
      <span className={cn("inline-flex items-center", className)} title={title}>
        {iconOnly}
      </span>
    );
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-normal",
        "bg-emerald-50 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-200/70 dark:border-emerald-800/70",
        className,
      )}
      title={title}
    >
      {iconOnly}
      <span>{labelText}</span>
    </span>
  );
}
