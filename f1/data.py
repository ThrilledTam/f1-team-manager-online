"""2026 grid data, extracted from the original YearsData.py (2026 section).

Teams, drivers, free agents, F2 drivers, queued and pool rookies are copied
verbatim (numbers, ages, salaries, perf, reliability, suppliers). ANSI colours
from the original were converted to hex for the web UI. No 2025 data is
included: the online version is 2026-only.
"""

GRID_2026 = {'teams': [{'name': 'McLaren', 'colour': '#EF9B33', 'money': 10.0, 'c_wins': 9, 'perf': 165.0, 'rel': 0.935, 'supplier': 'Mercedes', 'sup_stage': 1, 'd1': {'name': 'Lando Norris', 'age': 26, 'perf': 88, 'rep': 3.8, 'cha': 1, 'contract': 2, 'mv': 5.2, 'salary': 5.2}, 'd2': {'name': 'Oscar Piastri', 'age': 24, 'perf': 86, 'rep': 4.1, 'cha': 0, 'contract': 3, 'mv': 4.6, 'salary': 4.6}}, {'name': 'Mercedes', 'colour': '#75F1D3', 'money': 7.0, 'c_wins': 8, 'perf': 210.0, 'rel': 0.95, 'supplier': None, 'sup_stage': 0, 'd1': {'name': 'Kimi Antonelli', 'age': 19, 'perf': 87, 'rep': 3.4, 'cha': 0, 'contract': 2, 'mv': 2.0, 'salary': 2.0}, 'd2': {'name': 'George Russell', 'age': 28, 'perf': 86, 'rep': 4.1, 'cha': 0, 'contract': 2, 'mv': 4.9, 'salary': 4.9}}, {'name': 'Red Bull', 'colour': '#145984', 'money': 8.5, 'c_wins': 6, 'perf': 175.0, 'rel': 0.94, 'supplier': None, 'sup_stage': 0, 'd1': {'name': 'Max Verstappen', 'age': 28, 'perf': 91, 'rep': 4.4, 'cha': 4, 'contract': 3, 'mv': 7.2, 'salary': 7.2}, 'd2': {'name': 'Isack Hadjar', 'age': 21, 'perf': 85, 'rep': 3.0, 'cha': 0, 'contract': 1, 'mv': 0.5, 'salary': 0.5}}, {'name': 'Ferrari', 'colour': '#D52F38', 'money': 8.5, 'c_wins': 16, 'perf': 190.0, 'rel': 0.955, 'supplier': None, 'sup_stage': 0, 'd1': {'name': 'Lewis Hamilton', 'age': 41, 'perf': 87, 'rep': 4.0, 'cha': 7, 'contract': 2, 'mv': 6.7, 'salary': 6.7}, 'd2': {'name': 'Charles Leclerc', 'age': 28, 'perf': 87, 'rep': 3.9, 'cha': 0, 'contract': 1, 'mv': 5.0, 'salary': 5.0}}, {'name': 'Williams', 'colour': '#3267D4', 'money': 7.5, 'c_wins': 9, 'perf': 120.0, 'rel': 0.92, 'supplier': 'Mercedes', 'sup_stage': 1, 'd1': {'name': 'Carlos Sainz', 'age': 31, 'perf': 85, 'rep': 3.7, 'cha': 0, 'contract': 1, 'mv': 4.2, 'salary': 4.2}, 'd2': {'name': 'Alex Albon', 'age': 30, 'perf': 84, 'rep': 3.2, 'cha': 0, 'contract': 1, 'mv': 3.5, 'salary': 3.5}}, {'name': 'Racing Bulls', 'colour': '#7091F8', 'money': 4.0, 'c_wins': 0, 'perf': 160.0, 'rel': 0.94, 'supplier': 'Red Bull', 'sup_stage': 2, 'd1': {'name': 'Liam Lawson', 'age': 24, 'perf': 85, 'rep': 2.6, 'cha': 0, 'contract': 1, 'mv': 1.0, 'salary': 1.0}, 'd2': {'name': 'Arvid Lindblad', 'age': 18, 'perf': 80, 'rep': 2.0, 'cha': 0, 'contract': 1, 'mv': 0.5, 'salary': 0.5}}, {'name': 'Aston Martin', 'colour': '#4B9774', 'money': 8.0, 'c_wins': 0, 'perf': 125.0, 'rel': 0.89, 'supplier': None, 'sup_stage': 0, 'd1': {'name': 'Fernando Alonso', 'age': 44, 'perf': 86, 'rep': 3.3, 'cha': 2, 'contract': 1, 'mv': 4.0, 'salary': 4.0}, 'd2': {'name': 'Lance Stroll', 'age': 27, 'perf': 83, 'rep': 2.5, 'cha': 0, 'contract': 2, 'mv': 2.0, 'salary': 2.0}}, {'name': 'Haas', 'colour': '#D5D7D8', 'money': 6.5, 'c_wins': 0, 'perf': 140.0, 'rel': 0.93, 'supplier': 'Ferrari', 'sup_stage': 1, 'd1': {'name': 'Oliver Bearman', 'age': 21, 'perf': 85, 'rep': 3.2, 'cha': 0, 'contract': 1, 'mv': 1.0, 'salary': 1.0}, 'd2': {'name': 'Esteban Ocon', 'age': 29, 'perf': 82, 'rep': 2.2, 'cha': 0, 'contract': 1, 'mv': 2.0, 'salary': 2.0}}, {'name': 'Audi', 'colour': '#EB4526', 'money': 3.5, 'c_wins': 0, 'perf': 145.0, 'rel': 0.93, 'supplier': None, 'sup_stage': 0, 'd1': {'name': 'Nico Hülkenberg', 'age': 38, 'perf': 85, 'rep': 3.3, 'cha': 0, 'contract': 1, 'mv': 3.5, 'salary': 3.5}, 'd2': {'name': 'Gabriel Bortoleto', 'age': 21, 'perf': 82, 'rep': 2.4, 'cha': 0, 'contract': 1, 'mv': 2.0, 'salary': 2.0}}, {'name': 'Alpine', 'colour': '#479FE2', 'money': 7.0, 'c_wins': 2, 'perf': 150.0, 'rel': 0.935, 'supplier': 'Mercedes', 'sup_stage': 1, 'd1': {'name': 'Pierre Gasly', 'age': 30, 'perf': 86, 'rep': 3.0, 'cha': 0, 'contract': 3, 'mv': 3.0, 'salary': 3.0}, 'd2': {'name': 'Franco Colapinto', 'age': 22, 'perf': 82, 'rep': 1.9, 'cha': 0, 'contract': 1, 'mv': 1.5, 'salary': 1.5}}, {'name': 'Cadillac', 'colour': '#AAAAAD', 'money': 5.0, 'c_wins': 0, 'perf': 110.0, 'rel': 0.9, 'supplier': 'Ferrari', 'sup_stage': 1, 'd1': {'name': 'Sergio Pérez', 'age': 36, 'perf': 85, 'rep': 2.7, 'cha': 0, 'contract': 1, 'mv': 3.5, 'salary': 3.5}, 'd2': {'name': 'Valtteri Bottas', 'age': 36, 'perf': 84, 'rep': 2.9, 'cha': 0, 'contract': 1, 'mv': 2.7, 'salary': 2.7}}], 'free_agents': [{'name': 'Yuki Tsunoda', 'age': 26, 'perf': 86, 'rep': 2.8, 'cha': 0, 'contract': 0, 'mv': 2.7, 'salary': 2.7}, {'name': 'Jack Doohan', 'age': 23, 'perf': 82, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 1.5, 'salary': 1.5}, {'name': 'Kevin Magnussen', 'age': 33, 'perf': 83, 'rep': 2.1, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Daniel Ricciardo', 'age': 36, 'perf': 85, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 2.6, 'salary': 2.6}, {'name': 'Logan Sargeant', 'age': 25, 'perf': 81, 'rep': 2.1, 'cha': 0, 'contract': 0, 'mv': 1.7, 'salary': 1.7}, {'name': 'Guanyu Zhou', 'age': 26, 'perf': 84, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 2.6, 'salary': 2.6}, {'name': 'Nyck de Vries', 'age': 31, 'perf': 79, 'rep': 2.0, 'cha': 0, 'contract': 0, 'mv': 1.5, 'salary': 1.5}, {'name': 'Sebastian Vettel', 'age': 38, 'perf': 85, 'rep': 2.5, 'cha': 4, 'contract': 0, 'mv': 4.1, 'salary': 4.1}, {'name': 'Mick Schumacher', 'age': 27, 'perf': 85, 'rep': 2.5, 'cha': 0, 'contract': 0, 'mv': 3.0, 'salary': 3.0}, {'name': 'Nicholas Latifi', 'age': 29, 'perf': 80, 'rep': 2.0, 'cha': 0, 'contract': 0, 'mv': 1.7, 'salary': 1.7}, {'name': 'Antonio Giovinazzi', 'age': 32, 'perf': 82, 'rep': 2.1, 'cha': 0, 'contract': 0, 'mv': 2.5, 'salary': 2.5}, {'name': 'Romain Grosjean', 'age': 39, 'perf': 80, 'rep': 2.1, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Stoffel Vandoorne', 'age': 34, 'perf': 83, 'rep': 2.1, 'cha': 0, 'contract': 0, 'mv': 2.3, 'salary': 2.3}, {'name': "Pato O'Ward", 'age': 26, 'perf': 84, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 2.5, 'salary': 2.5}, {'name': 'Mathilda Paatz', 'age': 17, 'perf': 70, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Ryō Kirakawa', 'age': 31, 'perf': 83, 'rep': 2.1, 'cha': 0, 'contract': 0, 'mv': 1.8, 'salary': 1.8}, {'name': 'Felipe Drugovich', 'age': 25, 'perf': 84, 'rep': 2.5, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Théo Pourchaire', 'age': 22, 'perf': 82, 'rep': 2.2, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Robert Shwartzman', 'age': 25, 'perf': 84, 'rep': 2.2, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Marta García', 'age': 25, 'perf': 82, 'rep': 2.2, 'cha': 0, 'contract': 0, 'mv': 1.8, 'salary': 1.8}, {'name': 'Arthur Leclerc', 'age': 25, 'perf': 84, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 2.4, 'salary': 2.4}, {'name': 'Zane Maloney', 'age': 22, 'perf': 82, 'rep': 2.2, 'cha': 0, 'contract': 0, 'mv': 1.9, 'salary': 1.9}, {'name': 'Paul Aron', 'age': 22, 'perf': 84, 'rep': 2.5, 'cha': 0, 'contract': 0, 'mv': 1.9, 'salary': 1.9}, {'name': 'Frederik Vesti', 'age': 24, 'perf': 84, 'rep': 2.5, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Ayumu Iwasa', 'age': 23, 'perf': 82, 'rep': 2.3, 'cha': 0, 'contract': 0, 'mv': 2.1, 'salary': 2.1}, {'name': 'Abbi Pulling', 'age': 22, 'perf': 80, 'rep': 2.0, 'cha': 0, 'contract': 0, 'mv': 1.8, 'salary': 1.8}, {'name': 'Payton Westcott', 'age': 17, 'perf': 70, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Doriane Pin', 'age': 22, 'perf': 80, 'rep': 2.2, 'cha': 0, 'contract': 0, 'mv': 1.7, 'salary': 1.7}, {'name': 'Leonardo Fornaroli', 'age': 21, 'perf': 80, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 2.0, 'salary': 2.0}, {'name': 'Victor Martins', 'age': 24, 'perf': 84, 'rep': 2.5, 'cha': 0, 'contract': 0, 'mv': 1.9, 'salary': 1.9}, {'name': 'Jak Crawford', 'age': 21, 'perf': 81, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 1.9, 'salary': 1.9}, {'name': 'Richard Verschoor', 'age': 25, 'perf': 84, 'rep': 2.4, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}, {'name': 'Luke Browning', 'age': 24, 'perf': 85, 'rep': 2.6, 'cha': 0, 'contract': 0, 'mv': 2.2, 'salary': 2.2}], 'f2': [{'name': 'Alex Dunne', 'age': 20, 'perf': 80, 'rep': 1.8, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Dino Beganovic', 'age': 22, 'perf': 81, 'rep': 1.6, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Joshua Dürksen', 'age': 22, 'perf': 79, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Sebastián Montoya', 'age': 21, 'perf': 76, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Gabriele Minì', 'age': 21, 'perf': 79, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Rafael Villagómez', 'age': 24, 'perf': 78, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Oliver Goethe', 'age': 21, 'perf': 76, 'rep': 1.4, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Kush Maini', 'age': 25, 'perf': 80, 'rep': 1.6, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Ritomo Miyata', 'age': 26, 'perf': 79, 'rep': 1.7, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'John Bennett', 'age': 22, 'perf': 76, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Cian Shields', 'age': 20, 'perf': 75, 'rep': 1.4, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Rafael Câmara', 'age': 20, 'perf': 83, 'rep': 1.4, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Colton Herta', 'age': 26, 'perf': 80, 'rep': 2.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Noel León', 'age': 21, 'perf': 78, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Nikola Tsolov', 'age': 19, 'perf': 80, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Roman Bilinski', 'age': 21, 'perf': 76, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Mari Boya', 'age': 21, 'perf': 77, 'rep': 1.5, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Martinius Stenshorne', 'age': 20, 'perf': 78, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Tasanapol Inthraphuvasak', 'age': 20, 'perf': 77, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Emerson Fittipaldi', 'age': 19, 'perf': 77, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Nicolás Varrone', 'age': 25, 'perf': 75, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Laurens Van Hoepen', 'age': 20, 'perf': 77, 'rep': 1.2, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}], 'queued': [{'name': 'Matthew Taylor', 'age': 16, 'perf': 68, 'rep': 1.4, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Nicholas Buccella', 'age': 16, 'perf': 68, 'rep': 1.4, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Aiden Foster', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Max Turner', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Liam Harris', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}], 'pool': [{'name': 'Ethan Mitchell', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Noah Campbell', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Wyatt Archer', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Oscar Cruz', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Henry Hunter', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Patrick Turbostride', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Leo Sullivan', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Robert Turner', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Sebastian Harper', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Russell Bennett', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Aaron Adams', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Preston Palmer', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Lincoln Owens', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Murphy Cooper', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Chase Miller', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Oscar Clark', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Logan Williams', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Maddox Harris', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Orlando Hughes', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Vincent Hayes', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Seth Mitchell', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Cooper Lambert', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Max Porter', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': "Maximilian O'Connor", 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Austin Clarke', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Remington Shah', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Kennedy Black', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Phoenix Kumar', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Augustus Cooper', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Atticus Mahone', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Sullivan Taylor', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Orion Chavez', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Felipe Banks', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Carl Buccella', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Sam Turbostride', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Hugo Ramsey', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Milo Thompson', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Magnus Hamilton', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Theo Emerson', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Brian Wilson', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Felix Morgan', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Maxwell Vincent', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Lincoln Nelson', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Yoshimura Turner', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Mick Turbostride', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Sunny Harris', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Ford Mahone', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Arnold Wright', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Bodhi Wells', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Quinn Hamilton', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Adrian Voss', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Murphy Chavez', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Damon Winters', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Mark Porter', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Enzo Monroe', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Maximilian Whitmore', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Finn Callahan', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Sebastian Lancaster', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Logan Hayes', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Leonardo Ravensdale', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Ryker Maddox', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'George Harris', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Colson Drake', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Lewis Pulling', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Glenn Turbostride', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Malakai Sutter', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Ben Nelson', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}, {'name': 'Hunter Vail', 'age': 16, 'perf': 63, 'rep': 1.0, 'cha': 0, 'contract': 0, 'mv': 0.5, 'salary': 0.5}]}


# ---------------------------------------------------------------------------
# v0.4.2 — 2026 Test & Reserve drivers and Driver Academy assignments,
# from Attachment #1 (Project-Formula-2026-Dataset.csv). The CSV's
# "status" column is the guide: "Test & Reserve" drivers belong to the
# team in current_team; "F2" drivers WITH a current_team are that
# team's academy juniors. Names use the game's spelling ("Guanyu
# Zhou" — the CSV lists him as "Zhou Guanyu"); team names are the
# game's ("Red Bull", "Haas" — the CSV says "Red Bull Racing",
# "Haas F1 Team"). Age / salary (con_salary, $M/yr) / rating are the
# CSV values — a few differ from the GRID_2026 copies above (Westcott
# $1.4M not $0.5M, Kirakawa 84 not 83, Pourchaire 25 not 22, and the
# F2 academy salaries, which GRID_2026 lists as $0.5M for everyone);
# build_new_career() applies these overrides when it re-homes them.
# Reputation is NOT overridden — it stays as in GRID_2026.
# ---------------------------------------------------------------------------
TR_2026 = [
    {"name": "Yuki Tsunoda", "team": "Red Bull", "age": 26, "salary": 2.7, "perf": 86},
    {"name": "Jack Doohan", "team": "Haas", "age": 23, "salary": 1.5, "perf": 82},
    {"name": "Guanyu Zhou", "team": "Cadillac", "age": 26, "salary": 2.6, "perf": 84},
    {"name": "Nyck de Vries", "team": "McLaren", "age": 31, "salary": 1.5, "perf": 79},
    {"name": "Antonio Giovinazzi", "team": "Ferrari", "age": 32, "salary": 2.5, "perf": 82},
    {"name": "Stoffel Vandoorne", "team": "Aston Martin", "age": 34, "salary": 2.3, "perf": 83},
    {"name": "Pato O'Ward", "team": "McLaren", "age": 26, "salary": 2.5, "perf": 84},
    {"name": "Mathilda Paatz", "team": "Aston Martin", "age": 17, "salary": 0.5, "perf": 70},
    {"name": "Ryō Kirakawa", "team": "Haas", "age": 31, "salary": 1.8, "perf": 84},
    {"name": "Théo Pourchaire", "team": "Mercedes", "age": 25, "salary": 2.2, "perf": 82},
    {"name": "Robert Shwartzman", "team": "Audi", "age": 25, "salary": 2.2, "perf": 84},
    {"name": "Arthur Leclerc", "team": "Ferrari", "age": 25, "salary": 2.4, "perf": 84},
    {"name": "Paul Aron", "team": "Alpine", "age": 22, "salary": 1.9, "perf": 84},
    {"name": "Frederik Vesti", "team": "Mercedes", "age": 24, "salary": 2.2, "perf": 84},
    {"name": "Ayumu Iwasa", "team": "Red Bull", "age": 23, "salary": 2.1, "perf": 82},
    {"name": "Payton Westcott", "team": "Mercedes", "age": 17, "salary": 1.4, "perf": 70},
    {"name": "Doriane Pin", "team": "Mercedes", "age": 22, "salary": 1.7, "perf": 80},
    {"name": "Leonardo Fornaroli", "team": "McLaren", "age": 21, "salary": 2.0, "perf": 80},
    {"name": "Victor Martins", "team": "Williams", "age": 24, "salary": 1.9, "perf": 84},
    {"name": "Jak Crawford", "team": "Aston Martin", "age": 21, "salary": 1.9, "perf": 81},
    {"name": "Richard Verschoor", "team": "McLaren", "age": 25, "salary": 2.2, "perf": 84},
    {"name": "Luke Browning", "team": "Williams", "age": 24, "salary": 2.2, "perf": 85},
]

ACADEMY_2026 = [
    {"name": "Dino Beganovic", "team": "Ferrari", "age": 22, "salary": 1.5, "perf": 81},
    {"name": "Rafael Câmara", "team": "Ferrari", "age": 20, "salary": 1.0, "perf": 83},
    {"name": "Alex Dunne", "team": "Alpine", "age": 20, "salary": 1.5, "perf": 80},
    {"name": "Gabriele Minì", "team": "Alpine", "age": 21, "salary": 1.3, "perf": 79},
    {"name": "Kush Maini", "team": "Alpine", "age": 25, "salary": 1.3, "perf": 80},
    {"name": "Joshua Dürksen", "team": "Mercedes", "age": 22, "salary": 1.3, "perf": 79},
    {"name": "Ritomo Miyata", "team": "Haas", "age": 26, "salary": 1.3, "perf": 79},
    {"name": "Colton Herta", "team": "Cadillac", "age": 26, "salary": 2.4, "perf": 80},
    {"name": "Nikola Tsolov", "team": "Red Bull", "age": 19, "salary": 1.0, "perf": 80},
    {"name": "Mari Boya", "team": "Aston Martin", "age": 21, "salary": 1.0, "perf": 77},
]


# ---------------------------------------------------------------------------
# 2026 circuit calendar (v0.1). 12 rounds = 12 Grands Prix. Real circuits
# with their real lap counts; the same calendar repeats every season.
# ---------------------------------------------------------------------------
CALENDAR = [
    {"name": "Bahrain Grand Prix", "circuit": "Bahrain International Circuit", "laps": 57},
    {"name": "Saudi Arabian Grand Prix", "circuit": "Jeddah Corniche Circuit", "laps": 50},
    {"name": "Australian Grand Prix", "circuit": "Albert Park Circuit", "laps": 58},
    {"name": "Japanese Grand Prix", "circuit": "Suzuka Circuit", "laps": 53},
    {"name": "Chinese Grand Prix", "circuit": "Shanghai International Circuit", "laps": 56},
    {"name": "Miami Grand Prix", "circuit": "Miami International Autodrome", "laps": 57},
    {"name": "Emilia Romagna Grand Prix", "circuit": "Imola Circuit", "laps": 63},
    {"name": "Monaco Grand Prix", "circuit": "Circuit de Monaco", "laps": 78},
    {"name": "Spanish Grand Prix", "circuit": "Circuit de Barcelona-Catalunya", "laps": 66},
    {"name": "Canadian Grand Prix", "circuit": "Circuit Gilles Villeneuve", "laps": 70},
    {"name": "Austrian Grand Prix", "circuit": "Red Bull Ring", "laps": 71},
    {"name": "British Grand Prix", "circuit": "Silverstone Circuit", "laps": 52},
]


def circuit_for_round(round_no):
    """The Grand Prix for a 1-based round number (1..12)."""
    if 1 <= round_no <= len(CALENDAR):
        return CALENDAR[round_no - 1]
    return {"name": f"Round {round_no}", "circuit": "", "laps": 57}


# ---------------------------------------------------------------------------
# v0.3 — real 2025 standings, used as the initial order for season 1
# (2026). Like real F1, a new season opens in last season's finishing
# order, all on 0 points. The online version has no in-game 2025, so
# season 1 uses the real 2025 final standings (verified):
# drivers Norris 423, Verstappen 421, Piastri 410, Russell 319,
# Leclerc 242, Hamilton 156, Antonelli 150, Albon 73, Sainz 64,
# Alonso 56, Hülkenberg 51, Hadjar 51, Bearman 41, Lawson 38, Ocon 38,
# Stroll 33, Gasly 22, Bortoleto 19, Colapinto 0 (Tsunoda, 33 pts, is
# not on the 2026 grid — he starts 2026 as a free agent).
# The three 2026-grid drivers who did not race in F1 in 2025
# (Pérez, Bottas, Lindblad) go after them, ordered by perf.
# Constructors 2025: McLaren 833, Mercedes 469, Red Bull 451,
# Ferrari 398, Williams 137, Racing Bulls 92, Aston Martin 89,
# Haas 79, Sauber 70, Alpine 22. Audi takes Sauber's slot (9th) and
# Cadillac, a new team, goes last.
# ---------------------------------------------------------------------------
PREV_SEASON_DRIVER_ORDER = [
    "Lando Norris", "Max Verstappen", "Oscar Piastri", "George Russell",
    "Charles Leclerc", "Lewis Hamilton", "Kimi Antonelli", "Alex Albon",
    "Carlos Sainz", "Fernando Alonso", "Nico Hülkenberg", "Isack Hadjar",
    "Oliver Bearman", "Liam Lawson", "Esteban Ocon", "Lance Stroll",
    "Pierre Gasly", "Gabriel Bortoleto", "Franco Colapinto",
]

PREV_SEASON_TEAM_ORDER = [
    "McLaren", "Mercedes", "Red Bull", "Ferrari", "Williams",
    "Racing Bulls", "Aston Martin", "Haas", "Audi", "Alpine", "Cadillac",
]


# ---------------------------------------------------------------------------
# v0.3 — champion history, real 2010–2025 (from Matthew's PyCharm
# screenshot). Age = age at the start of the winning season.
# Trophies = the driver's CUMULATIVE career championships at that
# year — so Hamilton's 2014 row is x2 (he also won 2008). Winning
# team = that year's constructors champion, which is not always the
# drivers' champion's team (2021: Verstappen/Red Bull, Mercedes won
# the constructors'; 2024: Verstappen/Red Bull, McLaren won it).
# In-game seasons append a row as each season completes; 2026 is
# NOT pre-filled — it has not been played yet in the online version.
# (Matthew's screenshot shows a 2026 Antonelli row, but that is from
# his local save's simulated season, not canonical history.)
# ---------------------------------------------------------------------------
CHAMPION_HISTORY_2010_2025 = [
    {"year": 2010, "driver": "Sebastian Vettel", "age": 22, "team": "Red Bull", "points": 256, "trophies": 1, "winning_team": "Red Bull"},
    {"year": 2011, "driver": "Sebastian Vettel", "age": 23, "team": "Red Bull", "points": 392, "trophies": 2, "winning_team": "Red Bull"},
    {"year": 2012, "driver": "Sebastian Vettel", "age": 24, "team": "Red Bull", "points": 281, "trophies": 3, "winning_team": "Red Bull"},
    {"year": 2013, "driver": "Sebastian Vettel", "age": 25, "team": "Red Bull", "points": 397, "trophies": 4, "winning_team": "Red Bull"},
    {"year": 2014, "driver": "Lewis Hamilton", "age": 29, "team": "Mercedes", "points": 384, "trophies": 2, "winning_team": "Mercedes"},
    {"year": 2015, "driver": "Lewis Hamilton", "age": 30, "team": "Mercedes", "points": 381, "trophies": 3, "winning_team": "Mercedes"},
    {"year": 2016, "driver": "Nico Rosberg", "age": 30, "team": "Mercedes", "points": 385, "trophies": 1, "winning_team": "Mercedes"},
    {"year": 2017, "driver": "Lewis Hamilton", "age": 32, "team": "Mercedes", "points": 363, "trophies": 4, "winning_team": "Mercedes"},
    {"year": 2018, "driver": "Lewis Hamilton", "age": 33, "team": "Mercedes", "points": 408, "trophies": 5, "winning_team": "Mercedes"},
    {"year": 2019, "driver": "Lewis Hamilton", "age": 34, "team": "Mercedes", "points": 413, "trophies": 6, "winning_team": "Mercedes"},
    {"year": 2020, "driver": "Lewis Hamilton", "age": 35, "team": "Mercedes", "points": 347, "trophies": 7, "winning_team": "Mercedes"},
    {"year": 2021, "driver": "Max Verstappen", "age": 23, "team": "Red Bull", "points": 395.5, "trophies": 1, "winning_team": "Mercedes"},
    {"year": 2022, "driver": "Max Verstappen", "age": 24, "team": "Red Bull", "points": 454, "trophies": 2, "winning_team": "Red Bull"},
    {"year": 2023, "driver": "Max Verstappen", "age": 25, "team": "Red Bull", "points": 575, "trophies": 3, "winning_team": "Red Bull"},
    {"year": 2024, "driver": "Max Verstappen", "age": 26, "team": "Red Bull", "points": 437, "trophies": 4, "winning_team": "McLaren"},
    {"year": 2025, "driver": "Lando Norris", "age": 25, "team": "McLaren", "points": 423, "trophies": 1, "winning_team": "McLaren"},
]
