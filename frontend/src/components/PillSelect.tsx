// ---------------------------------------------------------------------------
// Transfera v2 — PillSelect Dropdown Component
// Custom Apple-styled dropdown select that adheres strictly to .agents/DESIGN.md:
// - Pill-shaped trigger with smooth chevron animation
// - Rounded-2xl (16px) floating menu with backdrop blur & soft elevation
// - Rounded-pill option items with hover states and active check indicators
// - Closes on click-outside and Escape key
// ---------------------------------------------------------------------------

import { useState, useRef, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronDown, Check } from "lucide-react";
import { cn } from "@/lib/utils";

export interface PillSelectOption {
  value: string;
  label: string;
  disabled?: boolean;
  title?: string;
}

export interface PillSelectProps {
  value: string;
  onChange: (value: string) => void;
  options: PillSelectOption[];
  placeholder?: string;
  className?: string;
  triggerClassName?: string;
  menuClassName?: string;
  align?: "left" | "right";
  title?: string;
  disabled?: boolean;
  size?: "sm" | "md";
}

export function PillSelect({
  value,
  onChange,
  options,
  placeholder = "Select...",
  className,
  triggerClassName,
  menuClassName,
  align = "left",
  title,
  disabled = false,
  size = "md",
}: PillSelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const selectedOption = options.find((opt) => opt.value === value);
  const displayLabel = selectedOption ? selectedOption.label : placeholder;
  const isSelected = value !== "";

  // Handle outside click
  useEffect(() => {
    if (!isOpen) return;

    const handlePointerDown = (e: PointerEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
      }
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setIsOpen(false);
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  const handleSelect = useCallback(
    (optValue: string) => {
      onChange(optValue);
      setIsOpen(false);
    },
    [onChange],
  );

  return (
    <div ref={containerRef} className={cn("relative inline-block", className)} title={title}>
      {/* Pill-shaped Trigger */}
      <button
        type="button"
        disabled={disabled}
        onClick={(e) => {
          e.stopPropagation();
          if (!disabled) setIsOpen((prev) => !prev);
        }}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        className={cn(
          "inline-flex items-center gap-2 rounded-pill font-normal border transition-all active:scale-[0.97]",
          size === "sm"
            ? "px-2.5 py-1 text-[11px]"
            : "pl-3.5 pr-2.5 py-1.5 text-xs",
          isOpen
            ? "border-action ring-2 ring-action/20 bg-muted/80 text-foreground"
            : isSelected
              ? "border-action/40 bg-action/5 hover:bg-action/10 text-foreground"
              : "border-border bg-muted/40 hover:bg-muted/70 text-foreground",
          disabled && "opacity-50 cursor-not-allowed pointer-events-none active:scale-100",
          triggerClassName,
        )}
      >
        <span className="truncate flex-1 text-left">{displayLabel}</span>
        <ChevronDown
          className={cn(
            "text-muted-foreground transition-transform duration-200 shrink-0",
            size === "sm" ? "w-2.5 h-2.5" : "w-3 h-3",
            isOpen && "rotate-180 text-action",
          )}
        />
      </button>

      {/* Dropdown Options Popup */}
      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ opacity: 0, y: -4, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.96 }}
            transition={{ duration: 0.15, ease: "easeOut" }}
            role="listbox"
            onClick={(e) => e.stopPropagation()}
            className={cn(
              "absolute top-full mt-1.5 z-50 min-w-[170px] max-w-[320px] p-1.5",
              "bg-popover/95 dark:bg-[#1a1c23]/95 backdrop-blur-md",
              "border border-border/80 rounded-2xl shadow-xl",
              align === "right" ? "right-0" : "left-0",
              menuClassName,
            )}
          >
            <div className="max-h-60 overflow-y-auto overscroll-contain space-y-0.5 pr-0.5 custom-scrollbar">
              {options.map((opt) => {
                const isItemActive = opt.value === value;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    role="option"
                    disabled={opt.disabled}
                    title={opt.title}
                    aria-selected={isItemActive}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!opt.disabled) handleSelect(opt.value);
                    }}
                    className={cn(
                      "w-full flex items-center justify-between gap-2 rounded-pill transition-colors text-left",
                      size === "sm" ? "px-2.5 py-1 text-[11px]" : "px-3 py-1.5 text-xs",
                      opt.disabled
                        ? "opacity-40 cursor-not-allowed text-muted-foreground"
                        : isItemActive
                          ? "bg-action text-white font-medium shadow-xs"
                          : "text-foreground hover:bg-muted/80 active:bg-muted",
                    )}
                  >
                    <span className="truncate">{opt.label}</span>
                    {isItemActive && (
                      <Check
                        className={cn(
                          "text-white shrink-0",
                          size === "sm" ? "w-3 h-3" : "w-3.5 h-3.5",
                        )}
                      />
                    )}
                  </button>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
