// ---------------------------------------------------------------------------
// Transfera v2 — Release Notes / What's New View
// Parses, resolves, and beautifully formats release notes adhering to
// Apple Design principles (SF Pro typography, bullet points, clean spacing,
// fallback handling, and external release link).
// ---------------------------------------------------------------------------

import { useEffect, useState } from "react";
import { ExternalLink, Loader2 } from "lucide-react";
import { openExternal } from "@/lib/desktop";
import { useUpdaterStore, isPlaceholderNotes } from "@/store/updater";

export interface ReleaseNotesViewProps {
  version: string;
  body?: string | null;
  className?: string;
  maxHeightClass?: string;
  compact?: boolean;
}

/**
 * Splits release notes into clean bullet items, handling:
 * - Semicolon-delimited lists (e.g. from release commit messages)
 * - Markdown bullet points (- or *)
 * - Multi-line release summaries
 */
export function parseReleaseNotes(raw?: string | null): string[] {
  if (!raw) return [];
  const trimmed = raw.trim();
  if (!trimmed) return [];

  // Remove full changelog URLs if trailing
  const withoutChangelogUrl = trimmed
    .replace(/\n*\*\*Full Changelog\*\*:\s*https:\/\/github\.com\/[^\s]+/gi, "")
    .trim();

  const lines = withoutChangelogUrl
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean);

  // If it's a single paragraph with semicolons (e.g. commit body with items separated by ';')
  if (lines.length === 1 && withoutChangelogUrl.includes(";")) {
    const items: string[] = [];
    let current = "";
    let parenDepth = 0;
    for (let i = 0; i < withoutChangelogUrl.length; i++) {
      const char = withoutChangelogUrl[i];
      if (char === "(" || char === "[" || char === "{") parenDepth++;
      else if (char === ")" || char === "]" || char === "}")
        parenDepth = Math.max(0, parenDepth - 1);

      if (char === ";" && parenDepth === 0) {
        const item = current.trim().replace(/^[-*•]\s*/, "");
        if (item) items.push(item);
        current = "";
      } else {
        current += char;
      }
    }
    const lastItem = current.trim().replace(/^[-*•]\s*/, "");
    if (lastItem) items.push(lastItem);

    if (items.length > 1) {
      return items.filter((it) => it.length > 0 && !/^(\.|\s)*$/.test(it));
    }
  }

  // If multiple lines, clean bullets / markdown headers
  const items: string[] = [];
  for (const line of lines) {
    if (/^#+\s*$/.test(line)) continue;
    const cleaned = line.replace(/^([*•\-+]|\d+\.)\s+/, "").trim();
    if (cleaned) {
      items.push(cleaned);
    }
  }

  return items.length > 0 ? items : [withoutChangelogUrl];
}

export default function ReleaseNotesView({
  version,
  body,
  className = "",
  maxHeightClass = "max-h-48",
  compact = false,
}: ReleaseNotesViewProps) {
  const resolveReleaseNotes = useUpdaterStore((s) => s.resolveReleaseNotes);
  const [loading, setLoading] = useState(false);
  const cleanVer = version.replace(/^v/, "");

  const isPlaceholder = isPlaceholderNotes(body, cleanVer);

  useEffect(() => {
    if (isPlaceholder) {
      setLoading(true);
      resolveReleaseNotes().finally(() => setLoading(false));
    }
  }, [isPlaceholder, resolveReleaseNotes]);

  const items = !isPlaceholder && body ? parseReleaseNotes(body) : [];
  const releaseUrl = `https://github.com/R1sh1m/Transfera/releases/tag/v${cleanVer}`;

  return (
    <div className={`space-y-2 text-foreground ${className}`}>
      <div
        className={`${maxHeightClass} overflow-y-auto pr-1 space-y-1.5 select-text`}
      >
        {loading ? (
          <div className="flex items-center gap-2 py-2 text-muted-foreground text-xs">
            <Loader2 className="w-3.5 h-3.5 animate-spin text-action" />
            <span>Loading release details...</span>
          </div>
        ) : items.length > 0 ? (
          <ul className={compact ? "space-y-1" : "space-y-1.5"}>
            {items.map((item, idx) => (
              <li
                key={idx}
                className="flex items-start gap-2 text-muted-foreground text-[11px] leading-relaxed"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-action/80 shrink-0 mt-1.5" />
                <span className="select-text">{item}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            Includes the latest performance improvements, security updates, and
            bug fixes for Transfera.
          </p>
        )}
      </div>

      <div className="pt-1.5 flex items-center justify-between border-t border-border/40 text-[10px] text-muted-foreground">
        <span>Transfera v{cleanVer}</span>
        <button
          type="button"
          onClick={() => openExternal(releaseUrl)}
          className="inline-flex items-center gap-1 text-action hover:underline cursor-pointer"
        >
          <span>View on GitHub</span>
          <ExternalLink className="w-3 h-3" />
        </button>
      </div>
    </div>
  );
}
