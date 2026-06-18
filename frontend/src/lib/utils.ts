import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export const isElectron: boolean =
  typeof window !== 'undefined' &&
  !!(window as unknown as Record<string, unknown>).electronAPI
