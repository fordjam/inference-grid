export async function renameView(name: string, next: string): Promise<Response> {
  return fetch(`/api/views/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "content-type": "text/plain" },
    body: next,
  });
}
