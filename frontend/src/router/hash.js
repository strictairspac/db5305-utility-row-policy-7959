export function route() {
  const raw = location.hash.slice(1) || 'cases';
  const [path, search = ''] = raw.split('?');
  const [key, id, ...rest] = path.split('/');
  return {
    key,
    id: Number(id) || 0,
    rest: rest.map(decodeURIComponent).filter(Boolean),
    query: Object.fromEntries(new URLSearchParams(search)),
  };
}
export function go(to) {
  location.hash = to;
}
