"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { ArrowLeft, BookOpen, Loader2 } from "lucide-react";

import { api, getErrorMessage, setTokens } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "@/components/ui/card";

type Mode = "login" | "forgot";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");
  const [loading, setLoading] = useState(false);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [forgotEmail, setForgotEmail] = useState("");

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const { data } = await api.post("/api/v1/users/login/", {
        email,
        password,
      });
      setTokens(data.access, data.refresh);
      toast.success(`Welcome back, ${data.user?.name ?? "there"}.`);
      router.push("/");
    } catch (error) {
      toast.error(getErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function handleForgotPassword(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      // The backend always returns the same generic message here, whether
      // or not the email is registered — deliberate, so this never reveals
      // account existence. See apps/users/views.py PasswordResetRequestView.
      const { data } = await api.post("/api/v1/users/password-reset/", {
        email: forgotEmail,
      });
      toast.success(
        data.detail ?? "If that email is registered, a reset link has been sent."
      );
      setForgotEmail("");
      setMode("login");
    } catch (error) {
      toast.error(getErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="relative flex flex-1 items-center justify-center overflow-hidden p-6">
      {/* soft violet backdrop, built entirely from `primary` at different opacities */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-40 -right-40 size-96 rounded-full bg-primary/20 blur-3xl"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -bottom-40 -left-40 size-96 rounded-full bg-primary/10 blur-3xl"
      />

      <Card className="relative w-full max-w-md border-primary/10 shadow-lg shadow-primary/5">
        <CardHeader className="items-center text-center">
          <div className="mb-2 flex size-11 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <BookOpen className="size-5" />
          </div>
          <CardTitle className="text-xl">
            {mode === "login" ? "Sign in" : "Reset your password"}
          </CardTitle>
          <CardDescription>
            {mode === "login"
              ? "Sign in to the library management system."
              : "Enter your email and we'll send you a reset link."}
          </CardDescription>
        </CardHeader>

        {mode === "login" ? (
          <form onSubmit={handleLogin} className="flex flex-col gap-4">
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <Label htmlFor="login-email">Email</Label>
                <Input
                  id="login-email"
                  type="email"
                  placeholder="you@library.com"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <Label htmlFor="login-password">Password</Label>
                  <button
                    type="button"
                    onClick={() => setMode("forgot")}
                    className="text-xs font-medium text-primary hover:text-primary/80 hover:underline"
                  >
                    Forgot password?
                  </button>
                </div>
                <Input
                  id="login-password"
                  type="password"
                  placeholder="••••••••"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            </CardContent>
            <CardFooter>
              <Button type="submit" className="w-full" disabled={loading}>
                {loading && <Loader2 className="size-4 animate-spin" />}
                Sign in
              </Button>
            </CardFooter>
          </form>
        ) : (
          <form onSubmit={handleForgotPassword} className="flex flex-col gap-4">
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <Label htmlFor="forgot-email">Email</Label>
                <Input
                  id="forgot-email"
                  type="email"
                  placeholder="you@library.com"
                  required
                  value={forgotEmail}
                  onChange={(e) => setForgotEmail(e.target.value)}
                />
              </div>
            </CardContent>
            <CardFooter className="flex-col gap-3">
              <Button type="submit" className="w-full" disabled={loading}>
                {loading && <Loader2 className="size-4 animate-spin" />}
                Send reset link
              </Button>
              <button
                type="button"
                onClick={() => setMode("login")}
                className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs font-medium"
              >
                <ArrowLeft className="size-3" />
                Back to sign in
              </button>
            </CardFooter>
          </form>
        )}
      </Card>
    </main>
  );
}
