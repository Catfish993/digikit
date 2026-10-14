/** Dev-only controls (diagnostics export, raw DSP image/state pickers, bench and LED preview) show on the dev server or with `?dev=1`. */
export const devEnabled = (): boolean => import.meta.env.DEV || new URLSearchParams(location.search).get('dev') === '1';
