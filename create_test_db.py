import sqlite3, random

db = sqlite3.connect('test_large.db')
c = db.cursor()

c.executescript('''
CREATE TABLE departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL,
    head_name TEXT,
    budget REAL,
    created_at TEXT
);
CREATE TABLE students (
    id INTEGER PRIMARY KEY,
    roll_no TEXT UNIQUE NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT UNIQUE,
    phone TEXT,
    dob TEXT,
    gender TEXT,
    address TEXT,
    city TEXT,
    department_id INTEGER,
    admission_year INTEGER,
    cgpa REAL,
    is_active INTEGER DEFAULT 1,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);
CREATE TABLE teachers (
    id INTEGER PRIMARY KEY,
    employee_id TEXT UNIQUE NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER,
    designation TEXT,
    salary REAL,
    joining_date TEXT,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);
CREATE TABLE courses (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    department_id INTEGER,
    credits INTEGER,
    semester INTEGER,
    teacher_id INTEGER,
    max_students INTEGER,
    FOREIGN KEY (department_id) REFERENCES departments(id),
    FOREIGN KEY (teacher_id) REFERENCES teachers(id)
);
CREATE TABLE enrollments (
    id INTEGER PRIMARY KEY,
    student_id INTEGER,
    course_id INTEGER,
    enrolled_at TEXT,
    grade TEXT,
    marks REAL,
    FOREIGN KEY (student_id) REFERENCES students(id),
    FOREIGN KEY (course_id) REFERENCES courses(id)
);
CREATE TABLE attendance (
    id INTEGER PRIMARY KEY,
    student_id INTEGER,
    course_id INTEGER,
    date TEXT,
    status TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id),
    FOREIGN KEY (course_id) REFERENCES courses(id)
);
CREATE TABLE fees (
    id INTEGER PRIMARY KEY,
    student_id INTEGER,
    amount REAL,
    fee_type TEXT,
    due_date TEXT,
    paid_date TEXT,
    status TEXT,
    transaction_id TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id)
);
CREATE TABLE exams (
    id INTEGER PRIMARY KEY,
    course_id INTEGER,
    exam_type TEXT,
    exam_date TEXT,
    total_marks REAL,
    duration_minutes INTEGER,
    FOREIGN KEY (course_id) REFERENCES courses(id)
);
CREATE TABLE exam_results (
    id INTEGER PRIMARY KEY,
    exam_id INTEGER,
    student_id INTEGER,
    marks_obtained REAL,
    grade TEXT,
    remarks TEXT,
    FOREIGN KEY (exam_id) REFERENCES exams(id),
    FOREIGN KEY (student_id) REFERENCES students(id)
);
CREATE TABLE library_books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    isbn TEXT UNIQUE,
    category TEXT,
    total_copies INTEGER,
    available_copies INTEGER,
    published_year INTEGER
);
CREATE TABLE book_issues (
    id INTEGER PRIMARY KEY,
    book_id INTEGER,
    student_id INTEGER,
    issue_date TEXT,
    due_date TEXT,
    return_date TEXT,
    fine REAL DEFAULT 0,
    FOREIGN KEY (book_id) REFERENCES library_books(id),
    FOREIGN KEY (student_id) REFERENCES students(id)
);
CREATE TABLE hostel_rooms (
    id INTEGER PRIMARY KEY,
    room_no TEXT UNIQUE,
    block TEXT,
    floor INTEGER,
    capacity INTEGER,
    room_type TEXT,
    monthly_rent REAL
);
CREATE TABLE hostel_allocations (
    id INTEGER PRIMARY KEY,
    room_id INTEGER,
    student_id INTEGER,
    allotment_date TEXT,
    vacate_date TEXT,
    status TEXT,
    FOREIGN KEY (room_id) REFERENCES hostel_rooms(id),
    FOREIGN KEY (student_id) REFERENCES students(id)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    title TEXT,
    description TEXT,
    event_date TEXT,
    venue TEXT,
    organizer TEXT,
    category TEXT,
    max_participants INTEGER
);
CREATE TABLE event_registrations (
    id INTEGER PRIMARY KEY,
    event_id INTEGER,
    student_id INTEGER,
    registered_at TEXT,
    status TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id),
    FOREIGN KEY (student_id) REFERENCES students(id)
);
''')

depts = [
    ('Computer Science', 'CS', 'Dr. Rajesh Kumar', 1200000),
    ('Electronics', 'EC', 'Dr. Priya Sharma', 980000),
    ('Mechanical', 'ME', 'Dr. Amit Patel', 870000),
    ('Civil', 'CV', 'Dr. Sunita Verma', 760000),
    ('Information Technology', 'IT', 'Dr. Vikram Singh', 1100000),
    ('Chemical', 'CH', 'Dr. Meera Joshi', 650000),
    ('Mathematics', 'MA', 'Dr. Ravi Gupta', 500000),
    ('Physics', 'PH', 'Dr. Anita Das', 480000),
]
for i, (name, code, head, budget) in enumerate(depts, 1):
    c.execute('INSERT INTO departments VALUES (?,?,?,?,?,?)',
              (i, name, code, head, budget, f'201{i}-06-01'))

designations = ['Professor', 'Associate Professor', 'Assistant Professor', 'Lecturer']
first_names = ['Rajesh','Priya','Amit','Sunita','Vikram','Meera','Ravi','Anita','Suresh','Kavita',
               'Mohan','Deepa','Arun','Sonal','Nitin','Pooja','Harsh','Nisha','Kiran','Dinesh']
last_names = ['Kumar','Sharma','Patel','Verma','Singh','Joshi','Gupta','Das','Shah','Mehta',
              'Yadav','Tiwari','Chauhan','Mishra','Pandey','Rao','Nair','Iyer','Pillai','Reddy']

for i in range(1, 81):
    fn = random.choice(first_names)
    ln = random.choice(last_names)
    c.execute('INSERT INTO teachers VALUES (?,?,?,?,?,?,?,?,?,?)',
              (i, f'EMP{1000+i}', fn, ln, f'{fn.lower()}{i}@college.edu',
               f'9{random.randint(100000000,999999999)}',
               random.randint(1,8), random.choice(designations),
               round(random.uniform(40000,120000),2),
               f'20{random.randint(5,20):02d}-{random.randint(1,12):02d}-{random.randint(1,28):02d}'))

course_names = ['Data Structures','Algorithms','DBMS','Operating Systems','Computer Networks',
                'Machine Learning','Web Development','Software Engineering','Digital Electronics',
                'Circuit Theory','Fluid Mechanics','Thermodynamics','Structural Analysis',
                'Calculus','Linear Algebra','Probability','Quantum Physics','Organic Chemistry',
                'Compiler Design','Computer Architecture','Microprocessors','VLSI Design',
                'Control Systems','Signal Processing','Heat Transfer','Manufacturing Processes']
for i, name in enumerate(course_names, 1):
    c.execute('INSERT INTO courses VALUES (?,?,?,?,?,?,?,?)',
              (i, f'CRS{100+i}', name, random.randint(1,8), random.randint(2,4),
               random.randint(1,8), random.randint(1,80), random.randint(40,80)))

student_first = ['Aarav','Vivaan','Aditya','Vihaan','Arjun','Sai','Reyansh','Ayaan','Krishna','Ishaan',
                 'Ananya','Diya','Aadhya','Saanvi','Lakshmi','Priya','Riya','Sneha','Pooja','Nisha',
                 'Rohan','Karan','Rahul','Nikhil','Amit','Suresh','Manish','Rajesh','Vijay','Sandeep']
cities = ['Mumbai','Delhi','Bangalore','Chennai','Hyderabad','Pune','Ahmedabad','Kolkata','Jaipur','Surat']
streets = ['MG Road','Station Road','Park Street','Civil Lines','Gandhi Nagar']

for i in range(1, 501):
    fn = random.choice(student_first)
    ln = random.choice(last_names)
    c.execute('INSERT INTO students VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (i, f'STU{2020000+i}', fn, ln, f'{fn.lower()}{i}@student.edu',
               f'9{random.randint(100000000,999999999)}',
               f'200{random.randint(0,9)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               random.choice(['M','F','M','F']),
               f'{random.randint(1,999)} {random.choice(streets)}',
               random.choice(cities), random.randint(1,8),
               random.randint(2018,2023), round(random.uniform(5.0,10.0),2), 1))

for i in range(1, 2001):
    c.execute('INSERT INTO enrollments VALUES (?,?,?,?,?,?)',
              (i, random.randint(1,500), random.randint(1,26),
               f'20{random.randint(20,23)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               random.choice(['A','B','C','D','F','A','B','B','A']),
               round(random.uniform(35,100),2)))

for i in range(1, 5001):
    c.execute('INSERT INTO attendance VALUES (?,?,?,?,?)',
              (i, random.randint(1,500), random.randint(1,26),
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               random.choice(['Present','Present','Present','Absent','Late'])))

fee_types = ['Tuition','Hostel','Library','Lab','Sports','Exam']
for i in range(1, 1501):
    paid = random.choice([True, True, False])
    c.execute('INSERT INTO fees VALUES (?,?,?,?,?,?,?,?)',
              (i, random.randint(1,500), round(random.uniform(5000,50000),2),
               random.choice(fee_types),
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}' if paid else None,
               'Paid' if paid else 'Pending',
               f'TXN{random.randint(100000,999999)}' if paid else None))

for i in range(1, 101):
    c.execute('INSERT INTO exams VALUES (?,?,?,?,?,?)',
              (i, random.randint(1,26),
               random.choice(['Mid-Term','End-Term','Quiz','Practical']),
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               random.choice([25,50,100]), random.choice([60,90,120,180])))

for i in range(1, 3001):
    obtained = round(random.uniform(30,100),2)
    grade = 'A' if obtained>=80 else 'B' if obtained>=65 else 'C' if obtained>=50 else 'D' if obtained>=40 else 'F'
    c.execute('INSERT INTO exam_results VALUES (?,?,?,?,?,?)',
              (i, random.randint(1,100), random.randint(1,500), obtained, grade,
               random.choice(['Good','Excellent','Needs Improvement','Average',None])))

book_titles = ['Introduction to Algorithms','Clean Code','Design Patterns','The Pragmatic Programmer',
               'Operating System Concepts','Computer Networks','Database System Concepts',
               'Artificial Intelligence','Machine Learning','Engineering Mathematics',
               'Signals and Systems','Control Systems Engineering','Fluid Mechanics',
               'Thermodynamics','Structural Analysis','Organic Chemistry','Calculus',
               'Linear Algebra','Physics Halliday','Compiler Design']
for i in range(1, 201):
    title = random.choice(book_titles) + f' Vol {random.randint(1,5)}'
    copies = random.randint(2,10)
    c.execute('INSERT INTO library_books VALUES (?,?,?,?,?,?,?,?)',
              (i, title, random.choice(last_names)+' et al.',
               f'978{random.randint(1000000000,9999999999)}',
               random.choice(['Engineering','Science','Mathematics','Reference']),
               copies, random.randint(1,copies), random.randint(1990,2023)))

for i in range(1, 801):
    returned = random.choice([True, True, False])
    c.execute('INSERT INTO book_issues VALUES (?,?,?,?,?,?,?)',
              (i, random.randint(1,200), random.randint(1,500),
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}' if returned else None,
               round(random.uniform(0,200),2) if not returned else 0))

for i in range(1, 101):
    block = random.choice(['A','B','C','D'])
    c.execute('INSERT INTO hostel_rooms VALUES (?,?,?,?,?,?,?)',
              (i, f'{block}-{i:03d}', block, random.randint(1,5),
               random.randint(2,4), random.choice(['Single','Double','Triple']),
               round(random.uniform(3000,8000),2)))

for i in range(1, 301):
    vacated = random.choice([True, False])
    c.execute('INSERT INTO hostel_allocations VALUES (?,?,?,?,?,?)',
              (i, random.randint(1,100), random.randint(1,500),
               f'20{random.randint(20,23)}-07-01',
               f'20{random.randint(21,24)}-06-30' if vacated else None,
               'Vacated' if vacated else 'Active'))

event_names = ['Tech Fest','Cultural Night','Sports Day','Hackathon','Guest Lecture',
               'Workshop on AI','Annual Day','Fresher Party','Alumni Meet','Blood Donation Camp']
for i in range(1, 51):
    c.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
              (i, f'{random.choice(event_names)} {2020+i%5}',
               'Annual college event for students and faculty.',
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               random.choice(['Main Auditorium','Ground','Seminar Hall','Lab Block']),
               random.choice(first_names)+' '+random.choice(last_names),
               random.choice(['Technical','Cultural','Sports','Academic']),
               random.randint(50,500)))

for i in range(1, 1001):
    c.execute('INSERT INTO event_registrations VALUES (?,?,?,?,?)',
              (i, random.randint(1,50), random.randint(1,500),
               f'20{random.randint(22,24)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}',
               random.choice(['Confirmed','Waitlisted','Cancelled','Confirmed','Confirmed'])))

db.commit()
db.close()

print('Done! Row counts:')
db = sqlite3.connect('test_large.db')
c = db.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
for (t,) in c.fetchall():
    c.execute(f'SELECT COUNT(*) FROM {t}')
    print(f'  {t}: {c.fetchone()[0]} rows')
db.close()
