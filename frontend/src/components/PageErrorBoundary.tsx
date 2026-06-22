// ---------------------------------------------------------------------------
// Transfera v2 — Page Error Boundary
// Catches rendering crashes in individual pages so one broken page doesn't
// take down the entire app shell (sidebar, titlebar, etc.).
// ---------------------------------------------------------------------------

import { Component, type ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

interface Props {
  pageName: string
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class PageErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(`[PageErrorBoundary: ${this.props.pageName}]`, error, info.componentStack)
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null })
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex-1 flex items-center justify-center px-6 py-12">
          <div className="bg-card border border-border rounded-lg p-8 max-w-md w-full text-center space-y-4">
            <div className="w-12 h-12 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center mx-auto">
              <AlertTriangle className="w-6 h-6 text-red-500" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-foreground">
                {this.props.pageName} failed to load
              </h2>
              <p className="text-sm text-muted-foreground mt-1">
                An error occurred while rendering this page.
              </p>
            </div>
            {this.state.error && (
              <details className="text-left">
                <summary className="text-xs text-muted-foreground cursor-pointer hover:text-foreground transition-colors">
                  Error details
                </summary>
                <pre className="mt-2 text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded p-3 overflow-auto max-h-40">
                  {this.state.error.message}
                  {'\n'}
                  {this.state.error.stack}
                </pre>
              </details>
            )}
            <button
              onClick={this.handleReset}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              <RefreshCw className="w-4 h-4" />
              Try Again
            </button>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
