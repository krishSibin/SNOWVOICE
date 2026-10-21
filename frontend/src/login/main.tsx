import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import AuthSwitch from "../components/ui/auth-switch";

// Plain form posts: the server sets the session cookie and redirects. After a
// failed attempt it sends us back with the form (portal) and the reason.
function Login() {
  const query = new URLSearchParams(location.search);
  return (
    <AuthSwitch
      portal={query.get("portal") === "admin" ? "admin" : "user"}
      failed={query.has("failed")}
      denied={query.has("denied")}
    />
  );
}

createRoot(document.getElementById("root")!).render(<Login />);
