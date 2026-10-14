// Recherche instantanée : chaque frappe lance immédiatement la recherche et les suggestions.
// La requête précédente encore en cours est annulée, et les réponses déjà reçues sont gardées
// en mémoire (retour arrière instantané). Sans JavaScript, le formulaire fonctionne normalement.
(() => {
  const formulaire = document.getElementById('recherche');
  const zone = document.getElementById('resultats');
  const champ = formulaire.elements.q;
  const liste = document.getElementById('suggestions');

  // --- Cache borné des réponses --------------------------------------------------------------

  function cache(taille) {
    const m = new Map();
    return {
      lire: (cle) => m.get(cle),
      ecrire: (cle, valeur) => {
        m.set(cle, valeur);
        if (m.size > taille) m.delete(m.keys().next().value);
      },
    };
  }
  const cacheResultats = cache(150);
  const cacheSuggestions = cache(300);

  // --- Résultats -------------------------------------------------------------------------------

  let requeteResultats = null;

  function parametres() {
    const p = new URLSearchParams(new FormData(formulaire));
    for (const [cle, valeur] of [...p.entries()]) if (!valeur) p.delete(cle);
    return p;
  }

  // Compteurs des tags (colonne de gauche) : nombre de résultats si on cochait ce tag en plus.
  // Les tags qui mèneraient à zéro résultat sont grisés, sauf s'ils sont déjà cochés.
  function majCompteurs() {
    const donnees = document.getElementById('compte-tags');
    if (!donnees) return;
    const comptes = JSON.parse(donnees.textContent);
    formulaire.querySelectorAll('input[name=tag]').forEach((caseTag) => {
      const nb = comptes[caseTag.value] || 0;
      const libelle = caseTag.closest('.case');
      libelle.querySelector('.nb').textContent = nb;
      libelle.classList.toggle('vide', nb === 0 && !caseTag.checked);
    });
  }

  function afficherResultats(html, p, defiler) {
    zone.innerHTML = html;
    zone.classList.remove('chargement');
    majCompteurs();
    dessinerFrise();
    const qs = p.toString();
    history.replaceState(null, '', qs ? '/?' + qs : '/');
    document.title = (p.get('q') ? p.get('q') + ' – ' : '') + 'GED DSI';
    // Seulement si le haut des résultats est sorti de l'écran : inutile de faire bouger la page sinon
    if (defiler && zone.getBoundingClientRect().top < 0) zone.scrollIntoView({ behavior: 'smooth' });
  }

  async function chercher(p = parametres(), defiler = false) {
    const cle = p.toString();
    if (requeteResultats) requeteResultats.abort();
    const dejaVu = cacheResultats.lire(cle);
    if (dejaVu !== undefined) return afficherResultats(dejaVu, p, defiler);

    requeteResultats = new AbortController();
    zone.classList.add('chargement');
    try {
      const reponse = await fetch('/resultats?' + cle, { signal: requeteResultats.signal });
      if (!reponse.ok) throw new Error(String(reponse.status));
      const html = await reponse.text();
      cacheResultats.ecrire(cle, html);
      afficherResultats(html, p, defiler);
    } catch (e) {
      if (e.name !== 'AbortError') formulaire.submit();
    }
  }

  // --- Frise des dates -------------------------------------------------------------------------
  // Courbe du nombre de documents trouvés par année de dernière modification (par mois quand
  // tout tient en 2 ans, ou quand la période choisie fait 2 ans au plus). Les comptes ignorent
  // la période choisie, pour voir ce qu'il y a autour. Chaque année (ou mois) est une colonne
  // cliquable, marquée d'un trait vertical discret ; les deux poignées choisissent un intervalle.
  // Masquée par défaut, ouverte d'office quand une période est choisie. Son aperçu (même courbe,
  // en petit, dans la barre de recherche) l'ouvre ou la ferme d'un clic.

  const frise = document.getElementById('frise');
  const boutonFrise = document.getElementById('afficher-frise');
  let friseOuverte = Boolean(formulaire.elements.du.value || formulaire.elements.au.value);
  const INITIALES_MOIS = 'JFMAMJJASOND';
  const NOMS_MOIS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.'];
  let periodes = [];
  let friseZoomee = false;

  const moisSuivant = (m) => {
    const [a, n] = m.split('-').map(Number);
    return n === 12 ? `${a + 1}-01` : `${a}-${String(n + 1).padStart(2, '0')}`;
  };
  const ecartMois = (a, b) => (+b.slice(0, 4) - +a.slice(0, 4)) * 12 + (+b.slice(5, 7) - +a.slice(5, 7));
  const libellePeriode = (cle) => (cle.length === 4 ? cle : `${NOMS_MOIS[+cle.slice(5) - 1]} ${cle.slice(0, 4)}`);

  // Courbe lissée passant par les points, sans dépasser leurs valeurs (cubique monotone,
  // Fritsch-Carlson) : elle ne descend jamais sous zéro entre deux périodes vides
  function cheminLisse(points) {
    const dx = [];
    const pentes = [];
    for (let i = 0; i < points.length - 1; i++) {
      dx.push(points[i + 1][0] - points[i][0]);
      pentes.push((points[i + 1][1] - points[i][1]) / dx[i]);
    }
    const tangentes = [pentes[0]];
    for (let i = 1; i < points.length - 1; i++) {
      tangentes.push(pentes[i - 1] * pentes[i] <= 0 ? 0
        : (3 * (dx[i - 1] + dx[i])) / ((2 * dx[i] + dx[i - 1]) / pentes[i - 1] + (dx[i] + 2 * dx[i - 1]) / pentes[i]));
    }
    tangentes.push(pentes[pentes.length - 1]);
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 0; i < points.length - 1; i++) {
      const [x0, y0] = points[i];
      const [x1, y1] = points[i + 1];
      const t = dx[i] / 3;
      d += `C${x0 + t},${y0 + tangentes[i] * t} ${x1 - t},${y1 - tangentes[i + 1] * t} ${x1},${y1}`;
    }
    return d;
  }

  // SVG de la courbe : 10 unités par colonne, étiré à la largeur de la frise (le trait garde
  // son épaisseur). La partie choisie est colorée, le reste grisé (découpe .frise-zone).
  // `decoupe` : identifiant du clipPath, distinct pour la frise et son aperçu.
  const HAUTEUR_COURBE = 30;
  function courbe(max, decoupe) {
    const n = periodes.length;
    const h = HAUTEUR_COURBE;
    const y = (nb) => (h - 1.5 - (nb / max) * (h - 4)).toFixed(2);
    const points = periodes.map((p, i) => [i * 10 + 5, +y(p.nb)]);
    // Prolongée à plat jusqu'aux bords
    points.unshift([0, points[0][1]]);
    points.push([n * 10, points[points.length - 1][1]]);
    const ligne = cheminLisse(points);
    return `<svg class="frise-courbe" viewBox="0 0 ${n * 10} ${h}" preserveAspectRatio="none" aria-hidden="true">
      <defs><clipPath id="${decoupe}"><rect class="frise-zone" x="0" y="0" width="${n * 10}" height="${h}"/></clipPath></defs>
      <path class="frise-aire" d="${ligne}L${n * 10},${h}L0,${h}Z" clip-path="url(#${decoupe})"/>
      <path class="frise-ligne grisee" d="${ligne}"/>
      <path class="frise-ligne" d="${ligne}" clip-path="url(#${decoupe})"/>
    </svg>`;
  }

  function dessinerFrise() {
    const donnees = document.getElementById('compte-mois');
    const comptes = donnees ? JSON.parse(donnees.textContent) : {};
    const mois = Object.keys(comptes).sort();
    frise.hidden = !mois.length || !friseOuverte;
    boutonFrise.hidden = !mois.length;
    if (!mois.length) return;

    const du = formulaire.elements.du.value;
    const au = formulaire.elements.au.value;
    // Bornes de la période choisie, au mois près ("2020" -> de 2020-01 à 2020-12)
    const debutChoisi = du && (du.length === 4 ? du + '-01' : du);
    const finChoisie = au && (au.length === 4 ? au + '-12' : au);
    friseZoomee = Boolean(debutChoisi && finChoisie && ecartMois(debutChoisi, finChoisie) < 24
                          && ecartMois(mois[0], mois[mois.length - 1]) >= 24);
    const parMois = friseZoomee || ecartMois(mois[0], mois[mois.length - 1]) < 24;

    periodes = [];
    if (parMois) {
      // Zoom : les années entières qui contiennent la période choisie
      let m = friseZoomee ? debutChoisi.slice(0, 4) + '-01' : mois[0];
      const fin = friseZoomee ? finChoisie.slice(0, 4) + '-12' : mois[mois.length - 1];
      for (; m <= fin; m = moisSuivant(m)) periodes.push({ cle: m, nb: comptes[m] || 0 });
    } else {
      for (let a = +mois[0].slice(0, 4); a <= +mois[mois.length - 1].slice(0, 4); a++) {
        const nb = mois.filter((m) => m.startsWith(a + '-')).reduce((t, m) => t + comptes[m], 0);
        periodes.push({ cle: String(a), nb });
      }
    }

    const n = periodes.length;
    const longueur = periodes[0].cle.length;
    let a = 0;
    let b = n - 1;
    if (du || au) {
      const debut = (debutChoisi || '0000').slice(0, longueur);
      const fin = (finChoisie || '9999').slice(0, longueur);
      a = periodes.findIndex((p) => p.cle >= debut);
      b = periodes.findLastIndex((p) => p.cle <= fin);
      if (a < 0) a = n - 1;
      if (b < 0) b = 0;
    }

    const max = Math.max(...periodes.map((p) => p.nb), 1);
    const pas = Math.ceil(n / 14);
    frise.style.setProperty('--n', n);
    frise.innerHTML = '';
    const entete = element('div', 'frise-entete');
    entete.append(element('span', 'frise-titre', '📅 Date de modification'), element('span', 'frise-choix'));
    if (du || au) {
      const toutes = element('button', 'frise-toutes', friseZoomee ? '⟲ Toutes les années' : '✕ Toutes les dates');
      toutes.type = 'button';
      entete.append(toutes);
    }
    const batons = element('div', 'frise-batons');
    const axe = element('div', 'frise-axe');
    periodes.forEach((p, i) => {
      const baton = element('button', 'frise-baton');
      baton.type = 'button';
      baton.dataset.i = i;
      baton.title = `${libellePeriode(p.cle)} : ${p.nb} document${p.nb > 1 ? 's' : ''}`;
      batons.append(baton);
      let etiquette = '';
      if (parMois) {
        const m = +p.cle.slice(5);
        etiquette = m === 1 || i === 0 ? p.cle.slice(0, 4) : (n <= 24 ? INITIALES_MOIS[m - 1] : '');
      } else if (i % pas === 0) {
        etiquette = p.cle;
      }
      axe.append(element('span', '', etiquette));
    });
    const poignees = element('div', 'frise-poignees');
    for (const [valeur, nom] of [[a, 'Début de la période'], [b, 'Fin de la période']]) {
      const curseur = element('input');
      Object.assign(curseur, { type: 'range', min: 0, max: n - 1, step: 1, value: valeur });
      curseur.setAttribute('aria-label', nom);
      poignees.append(curseur);
    }
    poignees.hidden = n < 2;
    batons.insertAdjacentHTML('afterbegin', courbe(max, 'frise-choix'));
    batons.append(poignees);
    frise.append(entete, batons, axe);
    // Aperçu : la courbe, et sous elle la période affichée ou choisie
    boutonFrise.innerHTML = courbe(max, 'mini-frise-choix');
    boutonFrise.append(element('span', 'mini-frise-libelle'));
    surlignerFrise(a, b, Boolean(du || au));
  }

  function bornesCurseurs() {
    const [c1, c2] = frise.querySelectorAll('.frise-poignees input');
    return [Math.min(+c1.value, +c2.value), Math.max(+c1.value, +c2.value)];
  }

  function surlignerFrise(a, b, choisie) {
    frise.classList.toggle('choisie', choisie);
    boutonFrise.classList.toggle('choisie', choisie);
    frise.querySelectorAll('.frise-baton').forEach((baton, i) => baton.classList.toggle('dans', i >= a && i <= b));
    for (const zone of [frise.querySelector('.frise-zone'), boutonFrise.querySelector('.frise-zone')]) {
      zone.setAttribute('x', choisie ? a * 10 : 0);
      zone.setAttribute('width', choisie ? (b - a + 1) * 10 : periodes.length * 10);
    }
    const nb = periodes.slice(a, b + 1).reduce((t, p) => t + p.nb, 0);
    const [du, au] = bornesPeriode(a, b);
    const libelle = !choisie ? 'Toutes les dates'
      : du === au ? libellePeriode(du)
      : `${libellePeriode(du)} → ${libellePeriode(au)}`;
    frise.querySelector('.frise-choix').textContent = `${libelle} · ${nb} document${nb > 1 ? 's' : ''}`;
    // Aperçu : l'étendue des dates tant que rien n'est choisi
    const etendue = `${libellePeriode(periodes[0].cle)} → ${libellePeriode(periodes[periodes.length - 1].cle)}`;
    boutonFrise.querySelector('.mini-frise-libelle').textContent = `📅 ${choisie ? libelle : etendue}`;
  }

  // Toute la frise (sans zoom) : pas de filtre
  const toutLaFrise = (a, b) => a === 0 && b === periodes.length - 1 && !friseZoomee;

  // Clés des bâtons a et b ; des mois couvrant des années entières deviennent ces années
  // (« 2020-01 » → « 2020-12 » : « 2020 »)
  function bornesPeriode(a, b) {
    const du = periodes[a].cle;
    const au = periodes[b].cle;
    return du.endsWith('-01') && au.endsWith('-12') ? [du.slice(0, 4), au.slice(0, 4)] : [du, au];
  }

  function choisirPeriode(a, b) {
    const [du, au] = toutLaFrise(a, b) ? ['', ''] : bornesPeriode(a, b);
    formulaire.elements.du.value = du;
    formulaire.elements.au.value = au;
  }

  boutonFrise.addEventListener('click', () => {
    friseOuverte = !friseOuverte;
    frise.hidden = !friseOuverte;
    boutonFrise.setAttribute('aria-expanded', friseOuverte);
  });

  frise.addEventListener('input', (e) => {
    if (e.target.type !== 'range') return;
    const [a, b] = bornesCurseurs();
    surlignerFrise(a, b, !toutLaFrise(a, b));
  });
  // Poignée relâchée : la recherche est relancée par l'écouteur « change » du formulaire,
  // qui passe après celui-ci (la frise est dans le formulaire)
  frise.addEventListener('change', (e) => {
    if (e.target.type === 'range') choisirPeriode(...bornesCurseurs());
  });
  frise.addEventListener('click', (e) => {
    const baton = e.target.closest('.frise-baton');
    if (baton) {
      choisirPeriode(+baton.dataset.i, +baton.dataset.i);
      chercher();
    } else if (e.target.closest('.frise-toutes')) {
      formulaire.elements.du.value = '';
      formulaire.elements.au.value = '';
      chercher();
    }
  });

  // --- Suggestions -----------------------------------------------------------------------------

  let requeteSuggestions = null;
  let elements = [];
  let actif = -1;

  const sansAccents = (t) => t.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

  function element(balise, classe, texte) {
    const e = document.createElement(balise);
    if (classe) e.className = classe;
    if (texte !== undefined) e.textContent = texte;
    return e;
  }

  // Chemin d'un document : début et dossier final toujours visibles, milieu réductible en « … »
  function filChemin(chemin) {
    const fil = element('span', 'fil-chemin');
    for (const partie of ['debut', 'milieu', 'fin']) {
      if (!chemin[partie].length) continue;
      const groupe = element('span', partie);
      for (const nom of chemin[partie]) groupe.append(element('span', 'sep', '›'), nom);
      fil.append(groupe);
    }
    fil.title = [...chemin.debut, ...chemin.milieu, ...chemin.fin].join(' › ');
    return fil;
  }

  function fermerSuggestions() {
    liste.hidden = true;
    champ.setAttribute('aria-expanded', 'false');
    actif = -1;
  }

  function selectionner(index) {
    actif = index;
    elements.forEach((el, i) => el.noeud.setAttribute('aria-selected', String(i === actif)));
    if (actif >= 0) elements[actif].noeud.scrollIntoView({ block: 'nearest' });
  }

  function choisir(choix) {
    fermerSuggestions();
    if (choix.type === 'document') {
      window.location = '/document/' + choix.id;
      return;
    }
    if (choix.type === 'personne') {
      // Les mots tapés servaient à trouver la personne : place à ses documents
      champ.value = '';
      filtrerPersonne([choix.nom], '');
      chercher(parametres(), true);
      return;
    }
    champ.value = choix.texte + ' ';
    champ.focus();
    chercher();
  }

  function afficherSuggestions(q, donnees) {
    liste.replaceChildren();
    elements = [];

    // Complétions : la partie déjà tapée en normal, le complément en gras (comme Google)
    const tape = sansAccents(q.trim());
    for (const texte of donnees.completions) {
      const noeud = element('div', 'suggestion completion');
      const longueur = texte.startsWith(tape) ? tape.length : 0;
      noeud.append(element('span', 'icone', '⌕'), element('span', 'texte'));
      noeud.lastChild.append(texte.slice(0, longueur), element('strong', '', texte.slice(longueur)));
      elements.push({ type: 'completion', texte, noeud });
    }

    if (donnees.documents.length) {
      elements.push({ titre: 'Documents' }); // intertitre, retiré de la navigation plus bas
      for (const d of donnees.documents) {
        const noeud = element('div', 'suggestion document');
        noeud.append(element('span', 'badge', d.famille), element('span', 'nom', d.nom), filChemin(d.chemin));
        elements.push({ type: 'document', id: d.id, noeud });
      }
    }

    // Personnes (auteur, modificateur, propriétaire) : un choix affiche leurs documents
    const personnes = donnees.personnes || [];
    if (personnes.length) {
      elements.push({ titre: 'Personnes' });
      for (const p of personnes) {
        const noeud = element('div', 'suggestion personne');
        noeud.append(element('span', 'icone', '👤'), element('span', 'nom', p.nom),
          element('span', 'nb', `${p.nb} document${p.nb > 1 ? 's' : ''}`));
        elements.push({ type: 'personne', nom: p.nom, noeud });
      }
    }

    for (const e of elements) {
      if (e.titre) {
        liste.append(element('div', 'suggestions-titre', e.titre));
        continue;
      }
      e.noeud.setAttribute('role', 'option');
      // mousedown plutôt que click : se déclenche avant que le champ perde le focus
      e.noeud.addEventListener('mousedown', (ev) => { ev.preventDefault(); choisir(e); });
      liste.append(e.noeud);
    }
    elements = elements.filter((e) => !e.titre);

    actif = -1;
    liste.hidden = elements.length === 0;
    champ.setAttribute('aria-expanded', String(!liste.hidden));
  }

  async function suggerer() {
    const q = champ.value;
    if (requeteSuggestions) requeteSuggestions.abort();
    if (!q.trim()) return fermerSuggestions();

    const dejaVu = cacheSuggestions.lire(q);
    if (dejaVu) return afficherSuggestions(q, dejaVu);

    requeteSuggestions = new AbortController();
    try {
      const reponse = await fetch('/suggestions?q=' + encodeURIComponent(q), { signal: requeteSuggestions.signal });
      const donnees = await reponse.json();
      cacheSuggestions.ecrire(q, donnees);
      if (champ.value === q && document.activeElement === champ) afficherSuggestions(q, donnees);
    } catch (e) {
      // requête annulée par une frappe plus récente
    }
  }

  // --- Événements ------------------------------------------------------------------------------

  champ.addEventListener('input', () => {
    suggerer();
    // Une seule lettre : seulement les suggestions (une recherche sur « p » ne sert à rien
    // et compte parmi les plus coûteuses : elle correspond à presque tous les documents)
    if (champ.value.trim().length !== 1) chercher();
  });
  champ.addEventListener('focus', () => { if (champ.value.trim()) suggerer(); });
  champ.addEventListener('blur', fermerSuggestions);
  champ.addEventListener('keydown', (e) => {
    // Échap ferme les suggestions mais ne vide jamais le champ (comportement natif de
    // type="search", même si la liste n'est pas encore affichée) : la croix sert à effacer
    if (e.key === 'Escape') {
      e.preventDefault();
      // Annule aussi une demande de suggestions en cours, qui rouvrirait la liste à son arrivée
      if (requeteSuggestions) requeteSuggestions.abort();
      fermerSuggestions();
      return;
    }
    if (liste.hidden || !elements.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectionner((actif + 1) % elements.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectionner(actif <= 0 ? elements.length - 1 : actif - 1);
    } else if (e.key === 'Enter' && actif >= 0) {
      e.preventDefault();
      choisir(elements[actif]);
    }
  });

  // Le champ « Personne » applique son filtre lui-même (au choix ou sur Entrée)
  formulaire.addEventListener('change', (e) => { if (e.target !== champ && e.target !== champPersonne) chercher(); });
  formulaire.addEventListener('submit', (e) => {
    e.preventDefault();
    fermerSuggestions();
    chercher();
  });

  zone.addEventListener('click', (e) => {
    // Dossiers seulement / tout déplier / tout replier, pour l'affichage en cours seulement
    // (l'affichage par défaut dépend du contexte : cf. _arbre.html). Au repli, les sources restent ouvertes.
    const deplier = e.target.closest('[data-deplier]');
    if (deplier) {
      const quoi = deplier.dataset.deplier;
      const arbre = zone.querySelector('.arbre-resultats');
      zone.querySelectorAll('details.arbre-dossier').forEach((d) => {
        d.open = quoi !== 'rien' || d.classList.contains('source');
      });
      if (quoi !== 'rien') {
        arbre.classList.toggle('dossiers-seuls', quoi === 'dossiers');
        zone.querySelectorAll('.fichiers-visibles').forEach((d) => d.classList.remove('fichiers-visibles'));
      }
      zone.querySelectorAll('[data-deplier]').forEach((b) => b.classList.toggle('actif', b === deplier));
      return;
    }
    // Nom d'un dossier de l'arborescence (ou chemin d'un résultat, ou ⬆) : on y entre, la recherche y est limitée
    const dossier = e.target.closest('[data-dossier]');
    if (dossier) {
      e.preventDefault(); // empêche aussi le dépliage du dossier quand on clique sur son nom
      formulaire.elements.dossier.value = dossier.dataset.dossier;
      chercher(parametres(), true); // on revient en haut des résultats, sinon on reste au fond de l'ancien arbre
      return;
    }
    // Mode « dossiers seulement » : le premier clic sur un dossier déplié affiche ses fichiers
    // au lieu de le replier (le clic suivant le replie, comme d'habitude)
    const entete = e.target.closest('.dossiers-seuls details.arbre-dossier > summary');
    if (entete) {
      const d = entete.parentElement;
      const aDesFichiers = d.querySelector(':scope > .arbre-contenu > .arbre-fichier');
      if (d.open && aDesFichiers && !d.classList.contains('fichiers-visibles')) {
        e.preventDefault();
        d.classList.add('fichiers-visibles');
      }
      return;
    }
    // Étiquettes des filtres actifs : la croix retire le filtre
    const retirerTag = e.target.closest('[data-retirer-tag]');
    const retirer = e.target.closest('[data-retirer]');
    if (retirerTag || retirer) {
      const quoi = retirer ? retirer.dataset.retirer : null;
      formulaire.querySelectorAll('input[name=tag]').forEach((c) => {
        if (quoi === 'tout' || (retirerTag && c.value === retirerTag.dataset.retirerTag)) c.checked = false;
      });
      if (quoi === 'pole' || quoi === 'tout') formulaire.elements.pole.value = '';
      if (quoi === 'famille' || quoi === 'tout') formulaire.elements.famille.value = '';
      if (quoi === 'dossier' || quoi === 'tout') formulaire.elements.dossier.value = '';
      if (quoi === 'personne' || quoi === 'tout') filtrerPersonne([], '');
      if (quoi === 'date' || quoi === 'tout') {
        formulaire.elements.du.value = '';
        formulaire.elements.au.value = '';
      }
      chercher();
      return;
    }
    const page = e.target.closest('a[data-page]');
    if (page) {
      e.preventDefault();
      chercher(new URLSearchParams(page.search), true);
      return;
    }
    // Clic sur un nom (auteur, dernier modificateur) : les documents de cette personne
    const personne = e.target.closest('a[data-personne]');
    if (personne) {
      e.preventDefault();
      champ.value = '';
      filtrerPersonne([personne.dataset.personne], personne.dataset.role);
      chercher(parametres(), true);
      return;
    }
    // Clic sur un tag d'un résultat : tous les documents portant ce tag (sans recharger la page)
    const tag = e.target.closest('a[data-tag]');
    const caseTag = tag && formulaire.querySelector(`input[name=tag][value="${tag.dataset.tag}"]`);
    if (caseTag) {
      e.preventDefault();
      champ.value = '';
      formulaire.querySelectorAll('input[name=tag]').forEach((c) => { c.checked = c === caseTag; });
      chercher(parametres(), true);
    }
  });

  // Filtre « documents d'une personne » : champs cachés personne (un par nom) et role
  function filtrerPersonne(noms, role) {
    const bloc = document.getElementById('filtre-personne');
    bloc.querySelectorAll('input[name=personne]').forEach((c) => c.remove());
    noms.forEach((nom) => {
      const c = element('input');
      Object.assign(c, { type: 'hidden', name: 'personne', value: nom });
      bloc.prepend(c);
    });
    formulaire.elements.role.value = noms.length ? role : '';
    champPersonne.value = noms[0] || '';
  }

  // Champ « Personne » des filtres : propositions pendant la frappe, filtre au choix (tous rôles)
  const champPersonne = document.getElementById('champ-personne');
  const listePersonnes = document.getElementById('liste-personnes');
  let propositions = [];
  let requetePersonnes = null;

  champPersonne.addEventListener('input', async () => {
    const q = champPersonne.value.trim();
    // Valeur choisie dans la liste : on filtre aussitôt
    if (propositions.some((p) => p.nom === champPersonne.value)) return appliquerPersonne();
    if (requetePersonnes) requetePersonnes.abort();
    if (q.length < 2) return;
    requetePersonnes = new AbortController();
    try {
      const reponse = await fetch('/personnes?' + new URLSearchParams({ q }), { signal: requetePersonnes.signal });
      propositions = await reponse.json();
      listePersonnes.replaceChildren(...propositions.map((p) => {
        const option = element('option');
        option.value = p.nom;
        option.label = `${p.nb} document${p.nb > 1 ? 's' : ''}`;
        return option;
      }));
    } catch (e) {
      if (e.name !== 'AbortError') propositions = [];
    }
  });

  function appliquerPersonne() {
    const saisi = champPersonne.value.trim();
    // Saisie partielle validée par Entrée : la première personne proposée
    const nom = !saisi ? '' : (propositions.find((p) => p.nom === saisi) || propositions[0] || { nom: saisi }).nom;
    filtrerPersonne(nom ? [nom] : [], '');
    chercher(parametres(), true);
  }

  champPersonne.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault(); // sinon Entrée enverrait tout le formulaire
    appliquerPersonne();
  });
  // Champ vidé (croix ou effacement) : plus de filtre
  champPersonne.addEventListener('search', () => { if (!champPersonne.value) appliquerPersonne(); });

  document.getElementById('effacer-filtres').addEventListener('click', () => {
    formulaire.querySelectorAll('select').forEach((s) => { s.value = ''; });
    formulaire.querySelectorAll('input[type=checkbox]').forEach((c) => { c.checked = false; });
    formulaire.elements.dossier.value = '';
    filtrerPersonne([], '');
    formulaire.elements.du.value = '';
    formulaire.elements.au.value = '';
    chercher();
  });

  // Curseur en fin de texte quand on revient sur une recherche existante
  champ.setSelectionRange(champ.value.length, champ.value.length);
  majCompteurs();
  dessinerFrise();
})();
