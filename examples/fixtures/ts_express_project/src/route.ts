export function validateBody(body: { name?: string }) {
  return typeof body.name === "string" && body.name.length > 0;
}
