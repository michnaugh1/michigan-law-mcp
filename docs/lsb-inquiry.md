# Draft inquiry to the Legislative Service Bureau

To: LITDEV@legislature.mi.gov (listed as managing editor and webmaster in the MCL updates feed; confirm this is the right office, and add the Legislative Service Bureau if policy questions belong there)
From: Michael C. Naughton, North Coast Legal, PLC, Traverse City, Michigan
Subject: Bulk access and reuse terms for the Michigan Compiled Laws

Hello,

I am a criminal defense attorney in Traverse City and I am building a private research tool for my
office and a small group of colleagues. It keeps a searchable local copy of the Michigan Compiled
Laws so that our AI research assistants can quote current statutory text accurately instead of
relying on memory. I have found the MCL updates RSS feed and plan to use it. Before I write anything that fetches section pages from legislature.mi.gov, I would like to do this
the way the Bureau prefers, and I have a few questions:

1. The site offers "Download" pages at each level (chapter, statute, section) with an HTML rendering
   (Home/RenderDoc?objectName=mcl-chap37) and a PDF (documents/mcl/pdf/MCL-CHAP37.pdf), and links the MCL
   updates feed in its footer. Is there a bulk or machine-readable form of the full MCL (XML, JSON, or a
   full-text export) beyond those? If the chapter-level downloads are the intended route for programmatic
   use, we would much prefer to fetch those (a few hundred files, refreshed only when the updates feed shows
   a change in that chapter) rather than crawl individual section pages. Which format should we rely on?
2. If not, what are the Bureau's terms for programmatic access to the website: acceptable request
   rates, permitted hours, and whether a registered contact address in our User-Agent is
   sufficient?
3. May we store a copy of the text in a private database and return it to our own users, with
   source attribution and a notice that it is an unofficial copy to be verified against the
   Bureau's publication? Are there wording or attribution requirements?
4. How often is the compiled text updated, and is there an "as of" date, change log, or feed of
   Public Acts that have been incorporated? Knowing this lets us poll at the right frequency.
5. Are prior versions of sections (for example, the text in effect on a past offense date)
   available in any structured form?

6. Some sections appear in the feed as variant nodes, for example mcl-333-16335-amended,
   mcl-333-16188-added and mcl-333-5474c[1]. What do these represent (for example, text enacted but not
   yet in effect, or a section enacted in more than one form), and how should a reader tell which text is
   currently in force?
7. About the MCL updates feed (MCLupdate.xml): every item in a build shares the build time as its
   pubDate. Is there a per-change date anywhere? How long do items stay in the feed, and does
   "added" also cover sections that were re-loaded without a change in text? Does an act-level
   "has been deleted" item mean the act was repealed, or that it is being re-loaded?

8. Requesting https://www.legislature.mi.gov/robots.txt returned an HTTP 502 (bad gateway) when we
   tried it by hand on September 21, 2026. Is that expected? Our crawler treats an unreadable
   robots.txt as "do not crawl", so we would like to confirm what automated access is permitted
   (and whether a robots.txt can be published) before fetching any section pages.

Separately, in case it helps your team: two independent clients we tried reported that they could not
verify the certificate chain for www.legislature.mi.gov (issuer DigiCert G2 TLS RSA SHA256 2020 CA1),
which can happen when a server omits its intermediate certificate. Browsers hide this.

Any guidance would be appreciated. I am glad to share what we build or to adjust our approach to
whatever works best for your office.

Thank you,
Michael C. Naughton
North Coast Legal, PLC
[phone] | mike@thenorthcoastlegal.com
