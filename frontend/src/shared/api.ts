export interface Me {
  email: string;
  name: string;
  role: string;
}

/** GET a JSON endpoint; a 401 sends the browser to the login page. */
export async function getJSON<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("signed out");
  }
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json() as Promise<T>;
}

export function when(seconds?: number | null): string {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "-";
}
