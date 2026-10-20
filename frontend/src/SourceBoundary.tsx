import { Component, type ReactNode } from 'react';
export default class SourceBoundary extends Component<{children: ReactNode; content: string}, {failed: boolean}> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <div><p role="alert">Source editor failed to load. Saved snapshot shown as plain text; reload to retry.</p><pre>{this.props.content}</pre></div> : this.props.children;
  }
}
