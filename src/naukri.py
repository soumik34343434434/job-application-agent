            try:
                from .credentials import save_naukri_credentials
                save_naukri_credentials(username, password)
            except Exception:
                pass
            return "ACCOUNT_CREATED_OR_CONFIRMED"
        return "REGISTRATION_NEEDS_ATTENTION"

    def activate_header_search(self) -> bool:
        """Open the current authenticated Naukri header search control."""
        # On the authenticated homepage the search form is already present in
        # the DOM, but collapsed behind .nI-gNb-sb__main. The searchAppWrapper
        # class belongs to the unrelated "Search appearances" section.
        candidates = [
            # Current authenticated Naukri homepage exposes the collapsed
            # search as a dedicated expand button. Avoid broad "Search" fallbacks:
            # those can submit a partially populated form and navigate away from
            # the homepage while activation is still being established.
            self.page.locator("button.nI-gNb-sb__expand"),
            self.page.get_by_role("button", name=re.compile(r"^search jobs here$", re.I)),
            self.page.locator('[aria-label="Search jobs here"]'),
            self.page.locator("div.nI-gNb-sb__main"),
            self.page.locator(".nI-gNb-sb__placeholder"),
            self.page.get_by_text(re.compile(r"^search jobs here$", re.I)),
        ]

        def search_controls_visible() -> bool:
            try:
                selectors = [
                    'input[placeholder="Enter keyword / designation / companies"]',
                    'input[placeholder="Select experience"]',
                    'input[placeholder="Enter location"]',
                ]
                for selector in selectors:
                    loc = self.page.locator(selector)
                    if not any(
                        loc.nth(i).is_visible() and loc.nth(i).is_enabled()
                        for i in range(loc.count())
                    ):
                        return False
                return True
            except Exception:
                return False

        # Fast path for the current authenticated Naukri header. Wait briefly
        # for the expand button because the header is hydrated after the page
        # shell appears. Then trigger its React handler directly so animation
        # stability cannot make the search surface flaky.
        try:
            expand = self.page.locator(
                'button.nI-gNb-sb__expand[aria-label="Search jobs here"]'
            ).first
            expand.wait_for(state="attached", timeout=5000)
            expand.wait_for(state="visible", timeout=5000)
            if expand.is_enabled():
                expand.evaluate("(e) => e.click()")
                self.page.wait_for_timeout(700)
                if search_controls_visible():
                    return True
        except Exception:
            pass

        for loc in candidates:
            try:
                for i in range(min(loc.count(), 20)):
                    candidate = loc.nth(i)
                    if not candidate.is_enabled():
                        continue

                    candidate.scroll_into_view_if_needed()
                    box = candidate.bounding_box()
                    if not box:
                        continue

                    # Prefer a normal Playwright click, then the actual center
                    # of the live wrapper, then a DOM click for React handlers.
                    clicked = False
                    try:
                        candidate.click(timeout=5000)
                        clicked = True
                    except Exception:
                        pass

                    if not clicked:
                        try:
                            self.page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            clicked = True
                        except Exception:
                            pass

                    if not clicked:
                        try: