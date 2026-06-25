export interface SourcePreviewPanelProps {
  sourcePath?: string | null
  deviceSource?: { device_id: string; device_path: string } | null
  onSelectionConfirm: (selectedPaths: string[]) => void
  onTransferStart?: (paths?: string[]) => void
}

declare const SourcePreviewPanel: React.FC<SourcePreviewPanelProps>
export default SourcePreviewPanel
