-- 后续演示输入：当前初始化版本尚不能执行 SQL。
CREATE TABLE student(id INT, name VARCHAR, age INT);
INSERT INTO student(id,name,age) VALUES (1,'Alice',20);
INSERT INTO student(name,age,id) VALUES ('张三',17,2);
SELECT id,name FROM student WHERE age >= 18;
DELETE FROM student WHERE id = 2;
SELECT * FROM student;
