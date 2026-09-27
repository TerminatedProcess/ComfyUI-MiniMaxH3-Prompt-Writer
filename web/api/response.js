function fallbackMessage(response, invalidSuccess = false) {
  const status = Number.isInteger(response?.status) ? ` (${response.status})` : "";
  if (invalidSuccess) {
    return `H3 Prompt Writer returned an invalid response${status}. ComfyUI may still be restarting.`;
  }
  // 404/405 with no JSON means the route is not registered at all -- the page is
  // newer than the process serving it. Saying "non-JSON response" sent people
  // hunting a bug in the feature instead of restarting the server.
  if (response?.status === 404 || response?.status === 405) {
    return `This server does not have that endpoint${status}. It is running an older build than the page: restart H3 Prompt Writer.`;
  }
  return `H3 Prompt Writer request failed${status}. The server returned a non-JSON response.`;
}

export async function readApiResponse(response) {
  const text = await response.text();
  let payload = null;
  if (text.trim()) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const error = new Error(payload?.error?.message || fallbackMessage(response));
    error.code = payload?.error?.code;
    error.details = payload?.error?.details;
    throw error;
  }
  if (payload === null) {
    throw new Error(fallbackMessage(response, true));
  }
  return payload;
}
