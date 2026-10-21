import { createRoot } from "react-dom/client";
import "../shared/styles.css";

// A plain form post: the server sets the session cookie and redirects.
function Login() {
  const failed = new URLSearchParams(location.search).has("failed");
  return (
    <div className="login-wrap">
      <form className="card login" method="post" action="/login">
        <h1>IT Help Desk</h1>
        <div className="muted small">Sign in to talk to the help desk assistant, or to open the ops console.</div>
        <label htmlFor="email">Email</label>
        <input id="email" name="email" type="email" autoComplete="username" required autoFocus />
        <label htmlFor="password">Password</label>
        <input id="password" name="password" type="password" autoComplete="current-password" required />
        {failed && <div className="error">That email and password don't match.</div>}
        <button className="primary" type="submit">Sign in</button>
      </form>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Login />);
